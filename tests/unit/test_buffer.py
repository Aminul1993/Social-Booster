from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
import respx

from services.buffer import BufferClient, BufferConfig, format_buffer_datetime
from services.errors import (
    PublisherAPIError,
    PublisherAuthError,
    PublisherError,
    ServiceNotConfiguredError,
)
from services.publishing import OAuthToken, PostRequest, PublishMode, SocialPublisher
from services.retry import RetryPolicy
from tests.helpers import (
    BUFFER_OAUTH_URL,
    BUFFER_POST_URL,
    BUFFER_PROFILES_URL,
    BUFFER_TOKEN_URL,
    PROFILES_PAYLOAD,
    form_body,
    query_params,
)

TOKEN = OAuthToken(access_token="1/abc")


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock_router:
        yield mock_router


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def make_client(http: httpx.AsyncClient, **overrides: object) -> BufferClient:
    values: dict[str, object] = {
        "client_id": "cid",
        "client_secret": "csecret",
        "redirect_uri": "http://localhost:8000/buffer/callback",
        "oauth_url": BUFFER_OAUTH_URL,
        "token_url": BUFFER_TOKEN_URL,
        "profiles_url": BUFFER_PROFILES_URL,
        "post_url": BUFFER_POST_URL,
        "retry": RetryPolicy(max_attempts=3, jitter=0),
    }
    values.update(overrides)
    return BufferClient(BufferConfig(**values), http, sleep=Sleeps())  # type: ignore[arg-type]


def schedule_post(**overrides: object) -> PostRequest:
    values: dict[str, object] = {
        "profile_ids": ("p1", "p2"),
        "text": "Hello world\n\n#a #b",
        "mode": PublishMode.SCHEDULE,
        "media_url": "https://social.example.com/uploads/x.png",
        "scheduled_at": datetime(2026, 11, 12, 15, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return PostRequest(**values)  # type: ignore[arg-type]


class TestOAuth:
    def test_implements_publisher_protocol(self, http: httpx.AsyncClient) -> None:
        assert isinstance(make_client(http), SocialPublisher)

    def test_authorization_url(self, http: httpx.AsyncClient) -> None:
        url = make_client(http, scope="write_public").authorization_url("state-123")
        assert url.startswith(BUFFER_OAUTH_URL + "?")
        assert query_params(url) == {
            "client_id": "cid",
            "redirect_uri": "http://localhost:8000/buffer/callback",
            "response_type": "code",
            "state": "state-123",
            "scope": "write_public",
        }

    def test_authorization_url_preserves_existing_query(self, http: httpx.AsyncClient) -> None:
        url = make_client(http, oauth_url=BUFFER_OAUTH_URL + "?x=1").authorization_url("s")
        assert "?x=1&client_id=cid" in url

    def test_authorization_url_requires_state(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(ValueError, match="state"):
            make_client(http).authorization_url("")

    def test_not_configured(self, http: httpx.AsyncClient) -> None:
        client = make_client(http, client_secret=None)
        assert not client.configured
        with pytest.raises(ServiceNotConfiguredError):
            client.authorization_url("s")

    async def test_exchange_code(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        route = router.post(BUFFER_TOKEN_URL).respond(
            json={"access_token": "1/new", "expires_in": 3600, "refresh_token": "r"}
        )
        token = await make_client(http).exchange_code("the-code")
        assert token.access_token == "1/new"
        assert token.refresh_token == "r"
        assert token.expires_at is not None
        assert not token.is_expired()
        request = route.calls.last.request
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert "Authorization" not in request.headers
        assert form_body(request) == {
            "client_id": ["cid"],
            "client_secret": ["csecret"],
            "redirect_uri": ["http://localhost:8000/buffer/callback"],
            "code": ["the-code"],
            "grant_type": ["authorization_code"],
        }

    async def test_exchange_code_without_expiry(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(json={"access_token": "1/x"})
        token = await make_client(http).exchange_code("c")
        assert token.expires_at is None

    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_exchange_code_rejected(
        self, router: respx.MockRouter, http: httpx.AsyncClient, status: int
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(status, json={"error": "invalid_grant"})
        with pytest.raises(PublisherAuthError, match="authorization code"):
            await make_client(http).exchange_code("c")

    @pytest.mark.parametrize("payload", [{}, {"access_token": ""}, ["x"]])
    async def test_exchange_code_missing_token(
        self, router: respx.MockRouter, http: httpx.AsyncClient, payload: object
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(json=payload)
        with pytest.raises(PublisherAuthError, match="access token"):
            await make_client(http).exchange_code("c")

    async def test_exchange_code_is_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_TOKEN_URL).respond(503)
        with pytest.raises(PublisherError, match="temporarily unavailable"):
            await make_client(http).exchange_code("c")
        assert route.call_count == 1

    async def test_exchange_code_requires_code(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(PublisherAuthError, match="authorization code"):
            await make_client(http).exchange_code("")


class TestProfiles:
    async def test_lists_profiles_from_array(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.get(BUFFER_PROFILES_URL).respond(
            json=[*PROFILES_PAYLOAD, {"id": "dead", "disabled": True}, {"service": "x"}, "junk"]
        )
        profiles = await make_client(http).list_profiles(TOKEN)
        assert [(p.id, p.service, p.username) for p in profiles] == [
            ("prof-ig", "instagram", "@acme"),
            ("prof-x", "twitter", "acme"),
        ]
        assert profiles[0].avatar_url == "https://cdn.buffer.test/a.png"
        assert profiles[1].avatar_url is None
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer 1/abc"
        assert "access_token" not in str(request.url)

    async def test_lists_profiles_from_object(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.get(BUFFER_PROFILES_URL).respond(
            json={"profiles": [{"_id": 7, "avatar": "http://insecure/a.png"}]}
        )
        (profile,) = await make_client(http).list_profiles(TOKEN)
        assert profile.id == "7"
        assert profile.service == "unknown"
        assert profile.username == "7"
        assert profile.avatar_url is None  # non-https avatars are dropped (mixed content)

    async def test_unexpected_shape(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.get(BUFFER_PROFILES_URL).respond(json={"profiles": "nope"})
        with pytest.raises(PublisherAPIError, match="Unexpected"):
            await make_client(http).list_profiles(TOKEN)

    async def test_retries_transient_errors(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.get(BUFFER_PROFILES_URL).mock(
            side_effect=[
                httpx.Response(502),
                httpx.ConnectTimeout("slow"),
                httpx.Response(200, json=PROFILES_PAYLOAD),
            ]
        )
        assert len(await make_client(http).list_profiles(TOKEN)) == 2
        assert route.call_count == 3

    async def test_auth_failure(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        router.get(BUFFER_PROFILES_URL).respond(401, json={"error": "invalid token"})
        with pytest.raises(PublisherAuthError, match="connect Buffer again"):
            await make_client(http).list_profiles(TOKEN)

    async def test_timeout_after_retries(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.get(BUFFER_PROFILES_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(PublisherError, match="in time"):
            await make_client(http).list_profiles(TOKEN)

    async def test_expired_token_is_rejected_locally(self, http: httpx.AsyncClient) -> None:
        expired = OAuthToken(access_token="t", expires_at=datetime.now(UTC) - timedelta(hours=1))
        with pytest.raises(PublisherAuthError, match="expired"):
            await make_client(http).list_profiles(expired)

    async def test_empty_token(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(PublisherAuthError, match="not connected"):
            await make_client(http).list_profiles(OAuthToken(access_token=""))


class TestPublish:
    async def test_schedule_payload(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_POST_URL).respond(
            json={"success": True, "updates": [{"id": "u1"}, {"id": "u2"}, {}], "message": "ok"}
        )
        result = await make_client(http).publish(TOKEN, schedule_post())
        assert result.update_ids == ("u1", "u2")
        assert result.message == "ok"
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer 1/abc"
        assert form_body(request) == {
            "profile_ids[]": ["p1", "p2"],
            "text": ["Hello world\n\n#a #b"],
            "media[photo]": ["https://social.example.com/uploads/x.png"],
            "media[thumbnail]": ["https://social.example.com/uploads/x.png"],
            "scheduled_at": ["2026-11-12T15:00:00Z"],
        }

    async def test_now_and_queue_modes(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_POST_URL).respond(json={"success": True, "update": {"id": "u"}})
        client = make_client(http)

        result = await client.publish(TOKEN, schedule_post(mode=PublishMode.NOW, scheduled_at=None))
        assert result.update_ids == ("u",)
        body = form_body(route.calls.last.request)
        assert body["now"] == ["true"]
        assert "scheduled_at" not in body

        await client.publish(
            TOKEN, schedule_post(mode=PublishMode.QUEUE, scheduled_at=None, media_url=None)
        )
        body = form_body(route.calls.last.request)
        assert "now" not in body
        assert "scheduled_at" not in body
        assert "media[photo]" not in body

    async def test_refused_by_buffer(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_POST_URL).respond(json={"success": False, "message": "Duplicate update"})
        with pytest.raises(PublisherAPIError, match="Duplicate update"):
            await make_client(http).publish(TOKEN, schedule_post())

    async def test_validation_error_from_buffer(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_POST_URL).respond(
            400, json={"message": "Text is too long", "code": 1025}
        )
        with pytest.raises(PublisherAPIError, match="Text is too long"):
            await make_client(http).publish(TOKEN, schedule_post())

    async def test_error_without_json(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_POST_URL).respond(404, text="not found")
        with pytest.raises(PublisherAPIError, match="HTTP 404"):
            await make_client(http).publish(TOKEN, schedule_post())

    async def test_invalid_json_success(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_POST_URL).respond(200, text="ok")
        with pytest.raises(PublisherAPIError, match="invalid response"):
            await make_client(http).publish(TOKEN, schedule_post())

    async def test_auth_error(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        router.post(BUFFER_POST_URL).respond(403)
        with pytest.raises(PublisherAuthError):
            await make_client(http).publish(TOKEN, schedule_post())

    async def test_server_error_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_POST_URL).respond(500)
        with pytest.raises(PublisherError, match="temporarily unavailable"):
            await make_client(http).publish(TOKEN, schedule_post())
        assert route.call_count == 1

    async def test_read_timeout_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_POST_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(PublisherError, match="in time"):
            await make_client(http).publish(TOKEN, schedule_post())
        assert route.call_count == 1  # the post may have been created: never duplicate

    async def test_connect_error_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_POST_URL).mock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={"success": True})]
        )
        result = await make_client(http).publish(TOKEN, schedule_post())
        assert result.update_ids == ()
        assert route.call_count == 2


def test_format_buffer_datetime() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert format_buffer_datetime(datetime(2026, 1, 2, 17, 30, tzinfo=plus_two)) == (
        "2026-01-02T15:30:00Z"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        format_buffer_datetime(datetime(2026, 1, 2, 17, 30))
