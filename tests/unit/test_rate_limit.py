from __future__ import annotations

import pytest

from app.config import RateLimitRule
from app.rate_limit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_allows_burst_then_limits_and_refills(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    rule = RateLimitRule(requests=3, period_seconds=60)
    assert [limiter.hit("k", rule) for _ in range(3)] == [None, None, None]

    retry_after = limiter.hit("k", rule)
    assert retry_after == pytest.approx(20.0)

    clock.now += 20
    assert limiter.hit("k", rule) is None
    assert limiter.hit("k", rule) is not None


def test_keys_are_isolated(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    rule = RateLimitRule(requests=1, period_seconds=10)
    assert limiter.hit("a", rule) is None
    assert limiter.hit("b", rule) is None
    assert limiter.hit("a", rule) is not None


def test_bucket_never_exceeds_capacity(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    rule = RateLimitRule(requests=2, period_seconds=1)
    limiter.hit("k", rule)
    clock.now += 3600
    assert [limiter.hit("k", rule) for _ in range(3)][-1] is not None


def test_prunes_refilled_buckets_when_full(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock, max_keys=3)
    rule = RateLimitRule(requests=5, period_seconds=10)
    for key in ("a", "b", "c"):
        limiter.hit(key, rule)
    clock.now += 60  # all buckets refilled -> stateless -> prunable
    limiter.hit("d", rule)
    assert len(limiter) == 1


def test_evicts_oldest_when_saturated(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock, max_keys=4)
    rule = RateLimitRule(requests=1, period_seconds=3600)
    for index, key in enumerate("abcd"):
        clock.now += index
        limiter.hit(key, rule)
    limiter.hit("e", rule)
    assert len(limiter) == 3  # oldest half evicted, then "e" added
