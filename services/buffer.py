"""Buffer integration: OAuth 2 authorization-code flow, profiles and posting.

Implements :class:`services.publishing.SocialPublisher` against Buffer's
publish API (v1):

* ``GET  {oauth_url}``     consent screen (``client_id``, ``redirect_uri``,
  ``response_type=code``, ``state``)
* ``POST {token_url}``     code -> access token (form-encoded)
* ``GET  {profiles_url}``  connected social profiles
* ``POST {post_url}``      create/schedule an update (form-encoded,
  ``profile_ids[]``, ``text``, ``media[photo]``, ``scheduled_at``...)

The access token is sent as an ``Authorization: Bearer`` header, never in a
URL, so it cannot leak into proxy or access logs. Every endpoint URL is
configurable.

Retries: profile listing (idempotent GET) is retried on timeouts/5xx/429.
Token exchange is never retried (authorization codes are single-use) and
posting is only retried when the connection failed *before* the request was
sent, so a retry can never create a duplicate post.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from services.errors import (
    PublisherAPIError,
    PublisherAuthError,
    PublisherError,
    ServiceNotConfiguredError,
)
from services.publishing import (
    OAuthToken,
    PostRequest,
    PublishingProfile,
    PublishMode,
    PublishResult,
)
from services.retry import (
    RETRYABLE_STATUS_CODES,
    RetryPolicy,
    SleepFunc,
    parse_retry_after,
    retry_async,
)

logger = logging.getLogger(__name__)

DEFAULT_OAUTH_URL = "https://bufferapp.com/oauth2/authorize"
DEFAULT_TOKEN_URL = "https://api.bufferapp.com/1/oauth2/token.json"  # noqa: S105 - a URL
DEFAULT_PROFILES_URL = "https://api.bufferapp.com/1/profiles.json"
DEFAULT_POST_URL = "https://api.bufferapp.com/1/updates/create.json"


@dataclass(frozen=True, slots=True)
class BufferConfig:
    """OAuth client credentials and endpoint URLs."""

    client_id: str | None
    client_secret: str | None
    redirect_uri: str
    oauth_url: str = DEFAULT_OAUTH_URL
    token_url: str = DEFAULT_TOKEN_URL
    profiles_url: str = DEFAULT_PROFILES_URL
    post_url: str = DEFAULT_POST_URL
    scope: str | None = None
    timeout_seconds: float = 20.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def format_buffer_datetime(value: datetime) -> str:
    """ISO-8601 UTC timestamp, the format Buffer's ``scheduled_at`` accepts."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BufferClient:
    """Buffer implementation of :class:`~services.publishing.SocialPublisher`."""

    provider = "buffer"
    display_name = "Buffer"

    def __init__(
        self,
        config: BufferConfig,
        http_client: httpx.AsyncClient,
        *,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        self.config = config
        self._http = http_client
        self._sleep = sleep

    # ----------------------------------------------------------------- config
    @property
    def configured(self) -> bool:
        return bool(
            self.config.client_id and self.config.client_secret and self.config.redirect_uri
        )

    def _require_configured(self) -> None:
        if not self.configured:
            raise ServiceNotConfiguredError(
                "Buffer is not configured (set BUFFER_CLIENT_ID and BUFFER_CLIENT_SECRET)."
            )

    @property
    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.config.timeout_seconds, connect=min(10.0, self.config.timeout_seconds)
        )

    # ------------------------------------------------------------------ OAuth
    def authorization_url(self, state: str) -> str:
        """Consent-screen URL; ``state`` must be validated on the callback."""
        self._require_configured()
        if not state:
            raise ValueError("state is required")
        params: dict[str, str] = {
            "client_id": self.config.client_id or "",
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self.config.scope:
            params["scope"] = self.config.scope
        separator = "&" if "?" in self.config.oauth_url else "?"
        return f"{self.config.oauth_url}{separator}{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthToken:
        """Trade the one-time authorization ``code`` for an access token."""
        self._require_configured()
        if not code:
            raise PublisherAuthError("Buffer did not return an authorization code.")
        data = {
            "client_id": self.config.client_id or "",
            "client_secret": self.config.client_secret or "",
            "redirect_uri": self.config.redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
        }
        response = await self._request("POST", self.config.token_url, data=data, auth=None)
        if response.status_code in (400, 401, 403):
            raise PublisherAuthError(
                "Buffer rejected the authorization code. Please connect again.",
                status_code=response.status_code,
            )
        payload = self._json_or_error(response, action="exchange the authorization code")
        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise PublisherAuthError("Buffer did not return an access token.")
        expires_at: datetime | None = None
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, int | float) and expires_in > 0:
            expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
        return OAuthToken(
            access_token=access_token,
            token_type=str(payload.get("token_type") or "bearer"),
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            scope=payload.get("scope"),
        )

    # --------------------------------------------------------------- profiles
    async def list_profiles(self, token: OAuthToken) -> list[PublishingProfile]:
        """Social profiles connected to the Buffer account."""
        self._check_token(token)

        async def attempt() -> list[PublishingProfile]:
            response = await self._request("GET", self.config.profiles_url, auth=token)
            self._raise_for_retryable(response)
            return self._parse_profiles(self._json_or_error(response, action="load profiles"))

        return await retry_async(
            attempt,
            policy=self.config.retry,
            is_retryable=lambda exc: isinstance(exc, PublisherError) and exc.retryable,
            sleep=self._sleep,
            description="Buffer profile listing",
        )

    @staticmethod
    def _parse_profiles(payload: Any) -> list[PublishingProfile]:
        items = payload.get("profiles", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise PublisherAPIError("Unexpected profile list from Buffer.")
        profiles: list[PublishingProfile] = []
        for item in items:
            if not isinstance(item, dict) or item.get("disabled"):
                continue
            profile_id = item.get("id") or item.get("_id")
            if not profile_id:
                continue
            username = (
                item.get("formatted_username")
                or item.get("service_username")
                or item.get("username")
                or item.get("formatted_service")
                or str(profile_id)
            )
            avatar = item.get("avatar_https") or item.get("avatar")
            profiles.append(
                PublishingProfile(
                    id=str(profile_id),
                    service=str(item.get("service") or "unknown"),
                    username=str(username),
                    avatar_url=(
                        str(avatar)
                        if isinstance(avatar, str) and avatar.startswith("https://")
                        else None
                    ),
                )
            )
        return profiles

    # ---------------------------------------------------------------- posting
    async def publish(self, token: OAuthToken, post: PostRequest) -> PublishResult:
        """Create a Buffer update (scheduled, queued or immediate)."""
        self._check_token(token)
        form: list[tuple[str, str]] = [("profile_ids[]", pid) for pid in post.profile_ids]
        form.append(("text", post.text))
        if post.media_url:
            # For image updates Buffer requires both photo and thumbnail.
            form.append(("media[photo]", post.media_url))
            form.append(("media[thumbnail]", post.media_url))
        if post.mode is PublishMode.SCHEDULE and post.scheduled_at is not None:
            form.append(("scheduled_at", format_buffer_datetime(post.scheduled_at)))
        elif post.mode is PublishMode.NOW:
            form.append(("now", "true"))

        async def attempt() -> httpx.Response:
            return await self._request("POST", self.config.post_url, data=form, auth=token)

        # Only connection failures (request never sent) are retried.
        response = await retry_async(
            attempt,
            policy=self.config.retry,
            is_retryable=lambda exc: isinstance(exc, PublisherError)
            and isinstance(exc.__cause__, httpx.ConnectError | httpx.ConnectTimeout),
            sleep=self._sleep,
            description="Buffer update creation",
        )
        payload = self._json_or_error(response, action="create the post")
        if not isinstance(payload, dict) or payload.get("success") is False:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise PublisherAPIError(f"Buffer refused the post: {message or 'unknown error'}.")
        updates = payload.get("updates") or ([payload["update"]] if payload.get("update") else [])
        update_ids = tuple(
            str(update.get("id"))
            for update in updates
            if isinstance(update, dict) and update.get("id")
        )
        logger.info(
            "Buffer update created",
            extra={
                "profiles": len(post.profile_ids),
                "mode": post.mode.value,
                "updates": len(update_ids),
            },
        )
        return PublishResult(update_ids=update_ids, message=payload.get("message"))

    # ---------------------------------------------------------------- helpers
    def _check_token(self, token: OAuthToken) -> None:
        if not token.access_token:
            raise PublisherAuthError("Buffer is not connected.")
        if token.is_expired():
            raise PublisherAuthError("Your Buffer authorization expired. Please connect again.")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        auth: OAuthToken | None,
        data: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if auth is not None:
            headers["Authorization"] = f"Bearer {auth.access_token}"
        try:
            # ``content`` + explicit header keeps list-of-tuples (repeated keys) intact.
            body = urlencode(data) if data is not None else None
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return await self._http.request(
                method, url, content=body, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise PublisherError("Buffer did not respond in time.", retryable=True) from exc
        except httpx.TransportError as exc:
            raise PublisherError("Could not reach Buffer.", retryable=True) from exc

    @staticmethod
    def _raise_for_retryable(response: httpx.Response) -> None:
        if response.status_code in RETRYABLE_STATUS_CODES:
            raise PublisherError(
                f"Buffer is temporarily unavailable (HTTP {response.status_code}).",
                retryable=True,
                status_code=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )

    @staticmethod
    def _error_message(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            for key in ("message", "error_description", "error"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _json_or_error(self, response: httpx.Response, *, action: str) -> Any:
        status = response.status_code
        if status in (401, 403):
            raise PublisherAuthError(
                "Buffer rejected the access token. Please connect Buffer again.",
                status_code=status,
            )
        if status >= 500 or status in (408, 429):
            raise PublisherError(
                f"Buffer is temporarily unavailable (HTTP {status}). Please try again.",
                status_code=status,
            )
        if response.is_error:
            detail = self._error_message(response)
            raise PublisherAPIError(
                f"Buffer could not {action}: {detail or f'HTTP {status}'}.", status_code=status
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PublisherAPIError(
                f"Buffer returned an invalid response while trying to {action}."
            ) from exc
