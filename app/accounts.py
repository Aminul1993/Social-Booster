"""The configured publishing account: connection status + profile caching.

Wraps a :class:`~services.publishing.SocialPublisher` that authenticates with
a server-wide personal access token, so every session shares one account and
one profile cache. When the provider rejects the token the cache is dropped
and the error is shown until the token is fixed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from services.errors import PublisherAuthError, PublisherError
from services.publishing import (
    PostRequest,
    PublishingProfile,
    PublishResult,
    SocialPublisher,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublisherStatus:
    """What the UI needs to render the connection widget and schedule forms."""

    provider: str
    display_name: str
    configured: bool
    connected: bool
    profiles: list[PublishingProfile] = field(default_factory=list)
    error: str | None = None

    @property
    def profile_ids(self) -> set[str]:
        return {profile.id for profile in self.profiles}


class PublisherAccounts:
    def __init__(
        self,
        publisher: SocialPublisher,
        *,
        cache_ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.publisher = publisher
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._cache: tuple[float, list[PublishingProfile]] | None = None

    @property
    def provider(self) -> str:
        return self.publisher.provider

    def clear_cache(self) -> None:
        self._cache = None

    # ---------------------------------------------------------------- profiles
    async def profiles(self, *, refresh: bool = False) -> list[PublishingProfile]:
        now = self._clock()
        if self._cache and not refresh and self._cache[0] > now:
            return self._cache[1]
        try:
            profiles = await self.publisher.list_profiles()
        except PublisherAuthError:
            self.clear_cache()
            raise
        if self._ttl > 0:
            self._cache = (now + self._ttl, profiles)
        return profiles

    async def status(self, *, refresh: bool = False) -> PublisherStatus:
        """Connection status; provider outages are reported, never raised."""

        def build(
            connected: bool,
            profiles: list[PublishingProfile] | None = None,
            error: str | None = None,
        ) -> PublisherStatus:
            return PublisherStatus(
                provider=self.provider,
                display_name=self.publisher.display_name,
                configured=self.publisher.configured,
                connected=connected,
                profiles=profiles or [],
                error=error,
            )

        if not self.publisher.configured:
            return build(connected=False)
        try:
            profiles = await self.profiles(refresh=refresh)
        except PublisherAuthError as exc:
            return build(connected=False, error=exc.message)
        except PublisherError as exc:
            logger.warning("Could not load publishing profiles", extra={"error": exc.message})
            return build(connected=True, error=exc.message)
        return build(connected=True, profiles=profiles)

    # ---------------------------------------------------------------- publish
    async def publish(self, post: PostRequest) -> PublishResult:
        try:
            return await self.publisher.publish(post)
        except PublisherAuthError:
            self.clear_cache()
            raise
