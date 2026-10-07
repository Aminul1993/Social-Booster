"""Connected publishing accounts: token persistence + profile caching.

Wraps a :class:`~services.publishing.SocialPublisher` with per-session token
storage. Expired access tokens are renewed with their refresh token on first
use. Whenever the provider rejects a token, the stored token is deleted so the
UI falls back to "Connect Buffer" instead of failing repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from app.repositories import TokenRepository
from services.errors import PublisherAuthError, PublisherError
from services.publishing import (
    OAuthToken,
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
        tokens: TokenRepository,
        *,
        cache_ttl_seconds: int = 300,
        max_cached_sessions: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.publisher = publisher
        self._tokens = tokens
        self._ttl = cache_ttl_seconds
        self._max_cached = max_cached_sessions
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, list[PublishingProfile]]] = OrderedDict()
        self._refresh_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @property
    def provider(self) -> str:
        return self.publisher.provider

    # ------------------------------------------------------------------ tokens
    async def token(self, session_id: str) -> OAuthToken | None:
        """The session's token, renewed first when it is (about to be) expired.

        Raises:
            PublisherError: the renewal failed for a transient reason.
        """
        token = await self._tokens.get(session_id, self.provider)
        if token is None or not token.is_expired():
            return token
        # Refresh tokens are single-use and reusing one revokes the grant, so
        # concurrent requests of a session must not refresh in parallel.
        async with self._refresh_lock(session_id):
            token = await self._tokens.get(session_id, self.provider)
            if token is None or not token.is_expired():
                return token  # renewed while we waited
            if not token.refresh_token:
                await self.disconnect(session_id)
                return None
            try:
                token = await self.publisher.refresh(token)
            except PublisherAuthError:
                await self.disconnect(session_id)
                return None
            await self._tokens.save(session_id, self.provider, token)
            return token

    def _refresh_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(session_id)
        if lock is None:
            lock = self._refresh_locks[session_id] = asyncio.Lock()
        return lock

    async def connect(self, session_id: str, code: str, *, code_verifier: str) -> OAuthToken:
        token = await self.publisher.exchange_code(code, code_verifier=code_verifier)
        await self._tokens.save(session_id, self.provider, token)
        self._cache.pop(session_id, None)
        return token

    async def disconnect(self, session_id: str) -> None:
        await self._tokens.delete(session_id, self.provider)
        self._cache.pop(session_id, None)

    # ---------------------------------------------------------------- profiles
    async def profiles(
        self, session_id: str, token: OAuthToken, *, refresh: bool = False
    ) -> list[PublishingProfile]:
        now = self._clock()
        cached = self._cache.get(session_id)
        if cached and not refresh and cached[0] > now:
            self._cache.move_to_end(session_id)
            return cached[1]
        try:
            profiles = await self.publisher.list_profiles(token)
        except PublisherAuthError:
            await self.disconnect(session_id)
            raise
        if self._ttl > 0:
            self._cache[session_id] = (now + self._ttl, profiles)
            self._cache.move_to_end(session_id)
            while len(self._cache) > self._max_cached:
                self._cache.popitem(last=False)
        return profiles

    async def status(self, session_id: str, *, refresh: bool = False) -> PublisherStatus:
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
            token = await self.token(session_id)
            if token is None:
                return build(connected=False)
            profiles = await self.profiles(session_id, token, refresh=refresh)
        except PublisherAuthError as exc:
            return build(connected=False, error=exc.message)
        except PublisherError as exc:
            logger.warning("Could not load publishing profiles", extra={"error": exc.message})
            return build(connected=True, error=exc.message)
        return build(connected=True, profiles=profiles)

    # ---------------------------------------------------------------- publish
    async def publish(self, session_id: str, token: OAuthToken, post: PostRequest) -> PublishResult:
        try:
            return await self.publisher.publish(token, post)
        except PublisherAuthError:
            await self.disconnect(session_id)
            raise
