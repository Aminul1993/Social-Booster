"""Buffer integration: API-key authentication, channels and posting.

Implements :class:`services.publishing.SocialPublisher` against Buffer's
current API (https://developers.buffer.com):

* ``POST {api_url}``  GraphQL: ``account.organizations`` -> ``channels`` (the
  social profiles) and ``createPost`` (one call per channel).

Requests authenticate with a personal API key (Buffer -> *Settings -> API ->
Create API key*). It acts on behalf of that one Buffer account, can reach all
of its organizations and channels, and does not expire until it is revoked, so
there is no consent screen, token exchange or refresh.

The key is sent as an ``Authorization: Bearer`` header, never in a URL, so it
cannot leak into proxy or access logs. The endpoint URL is configurable.

Retries: read queries are retried on timeouts/5xx/429. Posts are only retried
when the connection failed *before* the request was sent, so a retry can never
create a duplicate post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from services.errors import (
    PublisherAPIError,
    PublisherAuthError,
    PublisherError,
    ServiceNotConfiguredError,
)
from services.publishing import (
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

DEFAULT_API_URL = "https://api.buffer.com"

_REJECTED_MESSAGE = (
    "Buffer rejected the configured access token. Check BUFFER_ACCESS_TOKEN "
    "(Buffer -> Settings -> API)."
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
    """Personal access token (API key) and endpoint URL."""

    access_token: str | None
    api_url: str = DEFAULT_API_URL
    timeout_seconds: float = 20.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def format_buffer_datetime(value: datetime) -> str:
    """ISO-8601 UTC timestamp, the format Buffer's ``dueAt`` accepts."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        return bool(self.config.access_token)

    def _require_configured(self) -> None:
        if not self.configured:
            raise ServiceNotConfiguredError("Buffer is not configured (set BUFFER_ACCESS_TOKEN).")

    @property
    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.config.timeout_seconds, connect=min(10.0, self.config.timeout_seconds)
        )

    # --------------------------------------------------------------- profiles
    async def list_profiles(self) -> list[PublishingProfile]:
        """Channels (social profiles) of every organization of the account."""
        self._require_configured()
        data = await self._query(_ORGANIZATIONS_QUERY, action="load your account")
        account = data.get("account")
        organizations = account.get("organizations") if isinstance(account, dict) else None
        if not isinstance(organizations, list):
            raise PublisherAPIError("Unexpected account data from Buffer.")
        profiles: list[PublishingProfile] = []
        for organization in organizations:
            if not isinstance(organization, dict) or not organization.get("id"):
                continue
            data = await self._query(
                _CHANNELS_QUERY,
                {"organizationId": organization["id"]},
                action="load channels",
            )
            profiles.extend(self._parse_channels(data.get("channels")))
        return profiles

    async def _query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        action: str,
    ) -> dict[str, Any]:
        """Run a read-only GraphQL query, retrying transient failures."""
        return await retry_async(
            lambda: self._graphql(query, variables, action=action),
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
    async def publish(self, post: PostRequest) -> PublishResult:
        """Create one Buffer post per channel (scheduled, queued or immediate).

        Channels are independent: a refusal for one is reported in
        :attr:`PublishResult.failures` while the others still go out. Only when
        every channel fails is the first error raised.
        """
        self._require_configured()
        post_ids: list[str] = []
        failures: list[tuple[str, str]] = []
        first_error: PublisherError | None = None
        for channel_id in post.profile_ids:
            try:
                post_ids.append(await self._create_post(channel_id, post))
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

    async def _create_post(self, channel_id: str, post: PostRequest) -> str:
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
                _CREATE_POST_MUTATION, {"input": values}, action="create the post"
            ),
            policy=self.config.retry,
            is_retryable=lambda exc: (
                isinstance(exc, PublisherError)
                and isinstance(exc.__cause__, httpx.ConnectError | httpx.ConnectTimeout)
            ),
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
    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None,
        *,
        action: str,
    ) -> dict[str, Any]:
        response = await self._post({"query": query, "variables": variables or {}})
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
            return PublisherAuthError(_REJECTED_MESSAGE)
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

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }
        try:
            return await self._http.post(
                self.config.api_url,
                content=json.dumps(body),
                headers=headers,
                timeout=self._timeout,
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
            raise PublisherAuthError(_REJECTED_MESSAGE, status_code=status)
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
