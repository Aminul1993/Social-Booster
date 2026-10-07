"""Retry policy with exponential back-off, jitter and ``Retry-After`` support."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

#: HTTP statuses that are worth retrying for idempotent calls.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

SleepFunc = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often and how patiently an operation is retried.

    ``max_attempts`` counts the first call, so ``max_attempts=3`` means one call
    plus up to two retries.
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be >= 0")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait after the given (1-based) failed attempt."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        delay: float = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        # Jitter is for de-synchronising clients, not for security.
        return delay + random.uniform(0, delay * self.jitter)  # noqa: S311


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max((when - current).total_seconds(), 0.0)


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[BaseException], bool],
    sleep: SleepFunc = asyncio.sleep,
    description: str = "operation",
) -> T:
    """Run ``operation`` until it succeeds, fails permanently or attempts run out.

    The exception from the final attempt is re-raised unchanged so callers can
    map it to a user-facing error.
    """
    attempt = 1
    while True:
        try:
            return await operation()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            delay = policy.delay_for(attempt, getattr(exc, "retry_after", None))
            logger.warning(
                "Retrying %s after failure",
                description,
                extra={
                    "attempt": attempt,
                    "max_attempts": policy.max_attempts,
                    "delay_seconds": round(delay, 3),
                    "error": str(exc),
                },
            )
            await sleep(delay)
            attempt += 1
