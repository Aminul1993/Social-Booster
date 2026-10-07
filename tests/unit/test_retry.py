from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.errors import ServiceError
from services.retry import RetryPolicy, parse_retry_after, retry_async


class Recorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class TestPolicy:
    def test_exponential_backoff_with_cap(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=5.0, jitter=0.0)
        assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]

    def test_jitter_bounds(self) -> None:
        policy = RetryPolicy(base_delay=1.0, jitter=0.5)
        for _ in range(50):
            assert 1.0 <= policy.delay_for(1) <= 1.5

    def test_retry_after_overrides_and_is_capped(self) -> None:
        policy = RetryPolicy(max_delay=10.0)
        assert policy.delay_for(1, retry_after=3.0) == 3.0
        assert policy.delay_for(1, retry_after=100.0) == 10.0
        assert policy.delay_for(1, retry_after=-5) == 0.0

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_attempts": 0}, {"base_delay": -1}, {"jitter": 2}],
    )
    def test_validation(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match=r"must be"):
            RetryPolicy(**kwargs)  # type: ignore[arg-type]


class TestParseRetryAfter:
    def test_seconds(self) -> None:
        assert parse_retry_after("7") == 7.0

    def test_http_date(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert parse_retry_after("Thu, 01 Jan 2026 12:00:30 GMT", now=now) == 30.0

    def test_past_date_is_zero(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now=now) == 0.0

    def test_naive_date_assumed_utc(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert parse_retry_after("Thu, 01 Jan 2026 12:00:10 -0000", now=now) == 10.0

    @pytest.mark.parametrize("value", [None, "", "soon", "1.5"])
    def test_invalid(self, value: str | None) -> None:
        assert parse_retry_after(value) is None


class TestRetryAsync:
    async def test_succeeds_after_retryable_failures(self) -> None:
        sleep = Recorder()
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ServiceError("busy", retryable=True, retry_after=0.25)
            return "ok"

        result = await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=3),
            is_retryable=lambda exc: isinstance(exc, ServiceError) and exc.retryable,
            sleep=sleep,
        )
        assert result == "ok"
        assert attempts == 3
        assert sleep.delays == [0.25, 0.25]

    async def test_non_retryable_raises_immediately(self) -> None:
        sleep = Recorder()

        async def operation() -> None:
            raise ServiceError("nope")

        with pytest.raises(ServiceError, match="nope"):
            await retry_async(
                operation, policy=RetryPolicy(), is_retryable=lambda _: False, sleep=sleep
            )
        assert sleep.delays == []

    async def test_gives_up_after_max_attempts(self) -> None:
        sleep = Recorder()
        attempts = 0

        async def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise ServiceError("still busy", retryable=True)

        with pytest.raises(ServiceError, match="still busy"):
            await retry_async(
                operation,
                policy=RetryPolicy(max_attempts=4, jitter=0),
                is_retryable=lambda _: True,
                sleep=sleep,
            )
        assert attempts == 4
        assert len(sleep.delays) == 3
