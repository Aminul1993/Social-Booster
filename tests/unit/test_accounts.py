from __future__ import annotations

import pytest

from app.accounts import PublisherAccounts
from services.errors import PublisherAPIError, PublisherAuthError, PublisherError
from services.publishing import (
    PostRequest,
    PublishingProfile,
    PublishMode,
    PublishResult,
)

PROFILE = PublishingProfile(id="p1", service="instagram", username="@acme")


class FakePublisher:
    provider = "fake"
    display_name = "Fake"

    def __init__(self) -> None:
        self.configured = True
        self.profile_calls = 0
        self.profile_error: Exception | None = None
        self.publish_error: Exception | None = None

    async def list_profiles(self) -> list[PublishingProfile]:
        self.profile_calls += 1
        if self.profile_error:
            raise self.profile_error
        return [PROFILE]

    async def publish(self, post: PostRequest) -> PublishResult:
        if self.publish_error:
            raise self.publish_error
        return PublishResult(update_ids=("u1",))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def accounts(publisher: FakePublisher, clock: Clock) -> PublisherAccounts:
    return PublisherAccounts(publisher, cache_ttl_seconds=60, clock=clock)


async def test_status_when_configured(accounts: PublisherAccounts) -> None:
    status = await accounts.status()
    assert status.configured
    assert status.connected
    assert status.error is None
    assert status.profiles == [PROFILE]
    assert status.profile_ids == {"p1"}
    assert status.display_name == "Fake"
    assert accounts.provider == "fake"


async def test_profiles_are_cached_with_ttl(
    accounts: PublisherAccounts, publisher: FakePublisher, clock: Clock
) -> None:
    await accounts.profiles()
    await accounts.status()
    assert publisher.profile_calls == 1
    await accounts.profiles(refresh=True)
    assert publisher.profile_calls == 2
    clock.now += 61
    await accounts.profiles()
    assert publisher.profile_calls == 3


async def test_cache_disabled(publisher: FakePublisher) -> None:
    accounts = PublisherAccounts(publisher, cache_ttl_seconds=0)
    await accounts.profiles()
    await accounts.profiles()
    assert publisher.profile_calls == 2


async def test_rejected_token_is_reported(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    await accounts.profiles()
    publisher.profile_error = PublisherAuthError("revoked")
    status = await accounts.status(refresh=True)
    assert status.configured
    assert not status.connected
    assert status.error == "revoked"
    assert status.profiles == []


async def test_outage_is_reported_not_raised(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    publisher.profile_error = PublisherError("Buffer is down")
    status = await accounts.status()
    assert status.connected
    assert status.error == "Buffer is down"
    assert status.profiles == []


async def test_not_configured(accounts: PublisherAccounts, publisher: FakePublisher) -> None:
    publisher.configured = False
    status = await accounts.status()
    assert not status.configured
    assert not status.connected
    assert publisher.profile_calls == 0


async def test_profiles_auth_error_raises_and_clears_cache(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    await accounts.profiles()
    publisher.profile_error = PublisherAuthError("revoked")
    with pytest.raises(PublisherAuthError):
        await accounts.profiles(refresh=True)
    publisher.profile_error = None
    await accounts.profiles()
    assert publisher.profile_calls == 3  # nothing stale was served from the cache


async def test_publish_and_auth_failure(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    post = PostRequest(profile_ids=("p1",), text="hi", mode=PublishMode.NOW)
    assert (await accounts.publish(post)).update_ids == ("u1",)
    await accounts.profiles()
    assert publisher.profile_calls == 1

    publisher.publish_error = PublisherAPIError("rejected")
    with pytest.raises(PublisherAPIError):
        await accounts.publish(post)
    await accounts.profiles()
    assert publisher.profile_calls == 1  # a refused post keeps the cache

    publisher.publish_error = PublisherAuthError("revoked")
    with pytest.raises(PublisherAuthError):
        await accounts.publish(post)
    await accounts.profiles()
    assert publisher.profile_calls == 2  # a rejected token drops it
