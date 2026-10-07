from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.accounts import PublisherAccounts
from app.db import Database
from app.repositories import TokenRepository
from app.security import TokenCipher
from services.errors import PublisherAPIError, PublisherAuthError, PublisherError
from services.publishing import (
    OAuthToken,
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

    def authorization_url(self, state: str) -> str:
        return f"https://fake/auth?state={state}"

    async def exchange_code(self, code: str) -> OAuthToken:
        return OAuthToken(access_token=f"token-for-{code}")

    async def list_profiles(self, token: OAuthToken) -> list[PublishingProfile]:
        self.profile_calls += 1
        if self.profile_error:
            raise self.profile_error
        return [PROFILE]

    async def publish(self, token: OAuthToken, post: PostRequest) -> PublishResult:
        if self.publish_error:
            raise self.publish_error
        return PublishResult(update_ids=("u1",))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def tokens(tmp_path: Path) -> AsyncIterator[TokenRepository]:
    db = Database(tmp_path / "t.db")
    await db.connect()
    yield TokenRepository(db, TokenCipher.from_secrets(session_secret="z" * 40))
    await db.close()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def accounts(publisher: FakePublisher, tokens: TokenRepository, clock: Clock) -> PublisherAccounts:
    return PublisherAccounts(
        publisher, tokens, cache_ttl_seconds=60, clock=clock, max_cached_sessions=2
    )


async def test_connect_status_and_disconnect(accounts: PublisherAccounts) -> None:
    assert (await accounts.status("s")).connected is False
    token = await accounts.connect("s", "abc")
    assert token.access_token == "token-for-abc"
    status = await accounts.status("s")
    assert status.connected
    assert status.profiles == [PROFILE]
    assert status.profile_ids == {"p1"}
    assert status.display_name == "Fake"
    await accounts.disconnect("s")
    assert (await accounts.status("s")).connected is False


async def test_profiles_are_cached_with_ttl(
    accounts: PublisherAccounts, publisher: FakePublisher, clock: Clock
) -> None:
    token = await accounts.connect("s", "abc")
    await accounts.profiles("s", token)
    await accounts.profiles("s", token)
    assert publisher.profile_calls == 1
    await accounts.profiles("s", token, refresh=True)
    assert publisher.profile_calls == 2
    clock.now += 61
    await accounts.profiles("s", token)
    assert publisher.profile_calls == 3


async def test_cache_is_bounded(accounts: PublisherAccounts, publisher: FakePublisher) -> None:
    token = OAuthToken(access_token="t")
    for sid in ("a", "b", "c"):
        await accounts.profiles(sid, token)
    await accounts.profiles("a", token)  # evicted -> fetched again
    assert publisher.profile_calls == 4


async def test_cache_disabled(publisher: FakePublisher, tokens: TokenRepository) -> None:
    accounts = PublisherAccounts(publisher, tokens, cache_ttl_seconds=0)
    token = OAuthToken(access_token="t")
    await accounts.profiles("s", token)
    await accounts.profiles("s", token)
    assert publisher.profile_calls == 2


async def test_auth_error_disconnects(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    await accounts.connect("s", "abc")
    publisher.profile_error = PublisherAuthError("revoked")
    status = await accounts.status("s")
    assert not status.connected
    assert status.error == "revoked"
    assert await accounts.token("s") is None


async def test_outage_is_reported_not_raised(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    await accounts.connect("s", "abc")
    publisher.profile_error = PublisherError("Buffer is down")
    status = await accounts.status("s")
    assert status.connected
    assert status.error == "Buffer is down"
    assert status.profiles == []


async def test_not_configured(accounts: PublisherAccounts, publisher: FakePublisher) -> None:
    publisher.configured = False
    status = await accounts.status("s")
    assert not status.configured
    assert not status.connected


async def test_expired_token_is_dropped(
    accounts: PublisherAccounts, tokens: TokenRepository
) -> None:
    expired = OAuthToken(access_token="t", expires_at=datetime.now(UTC) - timedelta(minutes=5))
    await tokens.save("s", "fake", expired)
    assert await accounts.token("s") is None
    assert await tokens.get("s", "fake") is None


async def test_publish_and_auth_failure(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    token = await accounts.connect("s", "abc")
    post = PostRequest(profile_ids=("p1",), text="hi", mode=PublishMode.NOW)
    assert (await accounts.publish("s", token, post)).update_ids == ("u1",)

    publisher.publish_error = PublisherAPIError("rejected")
    with pytest.raises(PublisherAPIError):
        await accounts.publish("s", token, post)
    assert await accounts.token("s") is not None

    publisher.publish_error = PublisherAuthError("revoked")
    with pytest.raises(PublisherAuthError):
        await accounts.publish("s", token, post)
    assert await accounts.token("s") is None


async def test_profiles_auth_error_raises_and_disconnects(
    accounts: PublisherAccounts, publisher: FakePublisher
) -> None:
    token = await accounts.connect("s", "abc")
    publisher.profile_error = PublisherAuthError("revoked")
    with pytest.raises(PublisherAuthError):
        await accounts.profiles("s", token)
    assert await accounts.token("s") is None
