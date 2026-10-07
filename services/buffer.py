"""Buffer integration: OAuth 2 (authorization code + PKCE), channels and posting.

Implements :class:`services.publishing.SocialPublisher` against Buffer's
current API (https://developers.buffer.com):

* ``GET  {oauth_url}``  consent screen (``client_id``, ``redirect_uri``,
  ``response_type=code``, ``scope``, ``state``, S256 ``code_challenge``)
* ``POST {token_url}``  code -> access + refresh token, and refresh
  (form-encoded). Access tokens live about an hour; refresh tokens are
  single-use and rotate on every refresh.
* ``POST {api_url}``    GraphQL: ``account.organizations`` -> ``channels`` (the
  social profiles) and ``createPost`` (one call per channel).

Clients registered under Buffer's *Settings -> API* only exist on this API:
the retired v1 endpoints (``bufferapp.com/oauth2``, ``api.bufferapp.com/1``)
answer them with ``invalid_client``.

The access token is sent as an ``Authorization: Bearer`` header, never in a
URL, so it cannot leak into proxy or access logs. Every endpoint URL is
configurable.

Retries: read queries are retried on timeouts/5xx/429. Token requests are
never retried (codes and refresh tokens are single-use) and posts are only
retried when the connection failed *before* the request was sent, so a retry
can never create a duplicate post.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

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

DEFAULT_OAUTH_URL = "https://auth.buffer.com/auth"
DEFAULT_TOKEN_URL = "https://auth.buffer.com/token"  # noqa: S105 - a URL
DEFAULT_API_URL = "https://api.buffer.com"
#: Read organizations/channels, create posts, and get a refresh token.
DEFAULT_SCOPE = "account:read posts:write offline_access"

_EXPIRED_MESSAGE = "Your Buffer authorization expired. Please connect again."
_INVALID_CLIENT_MESSAGE = (
    "Buffer rejected this app's credentials (invalid_client). Check BUFFER_CLIENT_ID and "
    "BUFFER_CLIENT_SECRET, and that BUFFER_OAUTH_URL/BUFFER_TOKEN_URL point at auth.buffer.com."
)
_SHARE_MODES = {
    PublishMode.SCHEDULE: "customScheduled",
    PublishMode.QUEUE: "addToQueue",
    PublishMode.NOW: "shareNow",
}

_ORGANIZATIONS_QUERY = "query Organizations { account { organizations { id } } }"
_CHANNELS_QUERY = """
query Channels($organizationId: OrganizationId!) {
  channels(input: { organizationId: $organizationId }) {
    id name displayName service avatar isDisconnected isLocked
  }
}
"""
_CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}
"""


@dataclass(frozen=True, slots=True)
class BufferConfig:
    """OAuth client credentials and endpoint URLs."""

    client_id: str | None
    client_secret: str | None
    redirect_uri: str
    oauth_url: str = DEFAULT_OAUTH_URL
    token_url: str = DEFAULT_TOKEN_URL
    api_url: str = DEFAULT_API_URL
    scope: str | None = DEFAULT_SCOPE
    timeout_seconds: float = 20.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def format_buffer_datetime(value: datetime) -> str:
    """ISO-8601 UTC timestamp, the format Buffer's ``dueAt`` accepts."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pkce_challenge(code_verifier: str) -> str:
    """S256 ``code_challenge`` for a PKCE ``code_verifier`` (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_legacy_url(url: str) -> bool:
    """Whether ``url`` points at Buffer's retired v1 API (``bufferapp.com``)."""
    host = (urlparse(url).hostname or "").lower()
    return host == "bufferapp.com" or host.endswith(".bufferapp.com")


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
    def authorization_url(self, state: str, *, code_verifier: str) -> str:
        """Consent-screen URL; ``state`` must be validated on the callback."""
        self._require_configured()
        if not state:
            raise ValueError("state is required")
        params: dict[str, str] = {
            "client_id": self.config.client_id or "",
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        if self.config.scope:
            params["scope"] = self.config.scope
            if "offline_access" in self.config.scope.split():
                # OIDC Core 11: offline access (a refresh token) requires prompt=consent.
                params["prompt"] = "consent"
        separator = "&" if "?" in self.config.oauth_url else "?"
        return f"{self.config.oauth_url}{separator}{urlencode(params)}"

    async def exchange_code(self, code: str, *, code_verifier: str) -> OAuthToken:
        """Trade the one-time authorization ``code`` for a token pair."""
        self._require_configured()
        if not code:
            raise PublisherAuthError("Buffer did not return an authorization code.")
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": code_verifier,
            },
            rejected="Buffer rejected the authorization code. Please connect again.",
            action="exchange the authorization code",
        )

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        """Trade the single-use refresh token for a new token pair."""
        self._require_configured()
        if not token.refresh_token:
            raise PublisherAuthError(_EXPIRED_MESSAGE)
        refreshed = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": token.refresh_token},
            rejected=_EXPIRED_MESSAGE,
            action="renew the authorization",
        )
        if refreshed.refresh_token is None:  # not rotated: the old one stays valid
            return replace(refreshed, refresh_token=token.refresh_token)
        return refreshed

    async def _token_request(
        self, grant: dict[str, str], *, rejected: str, action: str
    ) -> OAuthToken:
        data = {
            "client_id": self.config.client_id or "",
            "client_secret": self.config.client_secret or "",
            **grant,
        }
        response = await self._request("POST", self.config.token_url, data=data, auth=None)
        if response.status_code in (400, 401, 403):
            invalid_client = self._error_field(response, "error") == "invalid_client"
            raise PublisherAuthError(
                _INVALID_CLIENT_MESSAGE if invalid_client else rejected,
                status_code=response.status_code,
            )
        payload = self._json_or_error(response, action=action)
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
        """Channels (social profiles) of every organization the user belongs to."""
        self._check_token(token)
        data = await self._query(token, _ORGANIZATIONS_QUERY, action="load your account")
        account = data.get("account")
        organizations = account.get("organizations") if isinstance(account, dict) else None
        if not isinstance(organizations, list):
            raise PublisherAPIError("Unexpected account data from Buffer.")
        profiles: list[PublishingProfile] = []
        for organization in organizations:
            if not isinstance(organization, dict) or not organization.get("id"):
                continue
            data = await self._query(
                token,
                _CHANNELS_QUERY,
                {"organizationId": organization["id"]},
                action="load channels",
            )
            profiles.extend(self._parse_channels(data.get("channels")))
        return profiles

    async def _query(
        self,
        token: OAuthToken,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        action: str,
    ) -> dict[str, Any]:
        """Run a read-only GraphQL query, retrying transient failures."""
        return await retry_async(
            lambda: self._graphql(token, query, variables, action=action),
            policy=self.config.retry,
            is_retryable=lambda exc: isinstance(exc, PublisherError) and exc.retryable,
            sleep=self._sleep,
            description=f"Buffer query ({action})",
        )

    @staticmethod
    def _parse_channels(items: Any) -> list[PublishingProfile]:
        if not isinstance(items, list):
            raise PublisherAPIError("Unexpected channel list from Buffer.")
        profiles: list[PublishingProfile] = []
        for item in items:
            # Disconnected channels need re-auth in Buffer; locked ones exceed the plan.
            if not isinstance(item, dict) or item.get("isDisconnected") or item.get("isLocked"):
                continue
            channel_id = item.get("id")
            if not channel_id:
                continue
            avatar = item.get("avatar")
            profiles.append(
                PublishingProfile(
                    id=str(channel_id),
                    service=str(item.get("service") or "unknown"),
                    username=str(item.get("displayName") or item.get("name") or channel_id),
                    avatar_url=(
                        avatar
                        if isinstance(avatar, str) and avatar.startswith("https://")
                        else None
                    ),
                )
            )
        return profiles

    # ---------------------------------------------------------------- posting
    async def publish(self, token: OAuthToken, post: PostRequest) -> PublishResult:
        """Create one Buffer post per channel (scheduled, queued or immediate).

        Channels are independent: a refusal for one is reported in
        :attr:`PublishResult.failures` while the others still go out. Only when
        every channel fails is the first error raised.
        """
        self._check_token(token)
        post_ids: list[str] = []
        failures: list[tuple[str, str]] = []
        first_error: PublisherError | None = None
        for channel_id in post.profile_ids:
            try:
                post_ids.append(await self._create_post(token, channel_id, post))
            except PublisherAuthError:
                raise
            except PublisherError as exc:
                first_error = first_error or exc
                failures.append((channel_id, exc.message))
        if first_error is not None and not post_ids:
            raise first_error
        logger.info(
            "Buffer posts created",
            extra={
                "channels": len(post.profile_ids),
                "mode": post.mode.value,
                "posts": len(post_ids),
            },
        )
        return PublishResult(update_ids=tuple(post_ids), failures=tuple(failures))

    async def _create_post(self, token: OAuthToken, channel_id: str, post: PostRequest) -> str:
        values: dict[str, Any] = {
            "channelId": channel_id,
            "text": post.text,
            "schedulingType": "automatic",
            "mode": _SHARE_MODES[post.mode],
        }
        if post.mode is PublishMode.SCHEDULE and post.scheduled_at is not None:
            values["dueAt"] = format_buffer_datetime(post.scheduled_at)
        if post.media_url:
            values["assets"] = [{"image": {"url": post.media_url}}]

        # Only connection failures (request never sent) are retried.
        data = await retry_async(
            lambda: self._graphql(
                token, _CREATE_POST_MUTATION, {"input": values}, action="create the post"
            ),
            policy=self.config.retry,
            is_retryable=lambda exc: isinstance(exc, PublisherError)
            and isinstance(exc.__cause__, httpx.ConnectError | httpx.ConnectTimeout),
            sleep=self._sleep,
            description="Buffer post creation",
        )
        result = data.get("createPost")
        if not isinstance(result, dict):
            raise PublisherAPIError("Buffer returned an invalid response while creating the post.")
        created = result.get("post")
        if isinstance(created, dict) and created.get("id"):
            return str(created["id"])
        message = result.get("message")
        raise PublisherAPIError(f"Buffer refused the post: {message or 'unknown error'}.")

    # ---------------------------------------------------------------- helpers
    def _check_token(self, token: OAuthToken) -> None:
        if not token.access_token:
            raise PublisherAuthError("Buffer is not connected.")
        if token.is_expired(leeway=0):
            raise PublisherAuthError(_EXPIRED_MESSAGE)

    async def _graphql(
        self,
        token: OAuthToken,
        query: str,
        variables: dict[str, Any] | None,
        *,
        action: str,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self.config.api_url,
            json_body={"query": query, "variables": variables or {}},
            auth=token,
        )
        self._raise_for_retryable(response)
        payload = self._json_or_error(response, action=action)
        if not isinstance(payload, dict):
            raise PublisherAPIError(
                f"Buffer returned an invalid response while trying to {action}."
            )
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            raise self._graphql_error(errors[0], action=action)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PublisherAPIError(
                f"Buffer returned an invalid response while trying to {action}."
            )
        return data

    @staticmethod
    def _graphql_error(error: Any, *, action: str) -> PublisherError:
        """Map a top-level GraphQL error (``extensions.code``) to a publisher error."""
        error = error if isinstance(error, dict) else {}
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if code in ("UNAUTHORIZED", "UNAUTHENTICATED"):
            return PublisherAuthError(
                "Buffer rejected the access token. Please connect Buffer again."
            )
        if code == "RATE_LIMIT_EXCEEDED":
            return PublisherError(
                "Buffer's rate limit was reached. Please try again in a minute.",
                retryable=True,
                status_code=429,
            )
        if code == "UNEXPECTED":
            return PublisherError(
                "Buffer is temporarily unavailable. Please try again.", retryable=True
            )
        message = error.get("message")
        detail = message if isinstance(message, str) and message else "unknown error"
        return PublisherAPIError(f"Buffer could not {action}: {detail}.")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        auth: OAuthToken | None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if auth is not None:
            headers["Authorization"] = f"Bearer {auth.access_token}"
        body: str | None = None
        if data is not None:
            body = urlencode(data)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body)
            headers["Content-Type"] = "application/json"
        try:
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
    def _error_field(response: httpx.Response, *keys: str) -> str | None:
        """First non-empty string among ``keys`` of a JSON error body."""
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            for key in keys:
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
            detail = self._error_field(response, "message", "error_description", "error")
            raise PublisherAPIError(
                f"Buffer could not {action}: {detail or f'HTTP {status}'}.", status_code=status
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PublisherAPIError(
                f"Buffer returned an invalid response while trying to {action}."
            ) from exc
