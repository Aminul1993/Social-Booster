"""In-process token-bucket rate limiting, exposed as FastAPI dependencies.

Each ``(scope, client IP)`` pair gets a bucket of ``requests`` tokens that
refills continuously over ``period_seconds``. Limits are per worker process;
for a multi-host deployment put a shared limiter (e.g. Redis or the reverse
proxy) in front - the dependency interface stays the same.

Client IPs come from ``request.client``; run uvicorn/gunicorn with
``--proxy-headers --forwarded-allow-ips=<proxy ip>`` behind a reverse proxy so
this is the real client address.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from app.config import RateLimitRule, RateLimitScope
from app.errors import RateLimitExceededError


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token buckets keyed by arbitrary strings."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    def __len__(self) -> int:
        return len(self._buckets)

    def hit(self, key: str, rule: RateLimitRule) -> float | None:
        """Consume one token. Returns ``None`` if allowed, else seconds to wait.

        Synchronous on purpose: there is no ``await`` between read and write, so
        it is atomic under asyncio without a lock.
        """
        now = self._clock()
        rate = rule.requests / rule.period_seconds
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._prune(now, rate, rule.requests)
            bucket = _Bucket(tokens=float(rule.requests), updated=now)
            self._buckets[key] = bucket
        else:
            bucket.tokens = min(rule.requests, bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return (1 - bucket.tokens) / rate

    def _prune(self, now: float, rate: float, capacity: int) -> None:
        """Drop buckets that have refilled completely (they carry no state)."""
        full = [
            key
            for key, bucket in self._buckets.items()
            if bucket.tokens + (now - bucket.updated) * rate >= capacity
        ]
        for key in full:
            del self._buckets[key]
        if len(self._buckets) >= self._max_keys:
            # Still saturated (e.g. under attack): evict the oldest half.
            oldest = sorted(self._buckets.items(), key=lambda item: item[1].updated)
            for key, _ in oldest[: len(oldest) // 2]:
                del self._buckets[key]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(scope: RateLimitScope) -> Callable[[Request], Awaitable[None]]:
    """Dependency factory: ``Depends(rate_limit("upload"))``."""

    async def dependency(request: Request) -> None:
        container = request.app.state.container
        settings = container.settings
        if not settings.rate_limit_enabled:
            return
        retry_after = container.rate_limiter.hit(
            f"{scope}:{client_ip(request)}", settings.rate_limit_rule(scope)
        )
        if retry_after is not None:
            container.metrics.rate_limited.labels(scope=scope).inc()
            raise RateLimitExceededError(retry_after=math.ceil(retry_after))

    dependency.__name__ = f"rate_limit_{scope}"
    return dependency
