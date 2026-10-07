from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from services.buffer import (
    BufferClient,
    BufferConfig,
    format_buffer_datetime,
    is_legacy_url,
    pkce_challenge,
)
from services.errors import (
    PublisherAPIError,
    PublisherAuthError,
    PublisherError,
    ServiceNotConfiguredError,
)
from services.publishing import OAuthToken, PostRequest, PublishMode, SocialPublisher
from services.retry import RetryPolicy
from tests.helpers import (
    BUFFER_API_URL,
    BUFFER_OAUTH_URL,
    BUFFER_TOKEN_URL,
    CHANNELS_PAYLOAD,
    FakeBufferAPI,
    form_body,
    query_params,
)

TOKEN = OAuthToken(access_token="1/abc")
VERIFIER = "v" * 43


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
        "redirect_uri": "https://localhost:8000/buffer/callback",
        "oauth_url": BUFFER_OAUTH_URL,
        "token_url": BUFFER_TOKEN_URL,
        "api_url": BUFFER_API_URL,
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


def graphql_body(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


class TestOAuth:
    def test_implements_publisher_protocol(self, http: httpx.AsyncClient) -> None:
        assert isinstance(make_client(http), SocialPublisher)

    def test_authorization_url(self, http: httpx.AsyncClient) -> None:
        url = make_client(http).authorization_url("state-123", code_verifier=VERIFIER)
        assert url.startswith(BUFFER_OAUTH_URL + "?")
        assert query_params(url) == {
            "client_id": "cid",
            "redirect_uri": "https://localhost:8000/buffer/callback",
            "response_type": "code",
            "state": "state-123",
            "code_challenge": pkce_challenge(VERIFIER),
            "code_challenge_method": "S256",
            "scope": "account:read posts:write offline_access",
            "prompt": "consent",
        }

    def test_consent_prompt_only_for_offline_access(self, http: httpx.AsyncClient) -> None:
        url = make_client(http, scope="account:read").authorization_url("s", code_verifier="v")
        assert query_params(url)["scope"] == "account:read"
        assert "prompt" not in query_params(url)
        no_scope = make_client(http, scope=None).authorization_url("s", code_verifier="v")
        assert "scope" not in query_params(no_scope)

    def test_pkce_challenge_rfc7636_vector(self) -> None:
        # RFC 7636, appendix B.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    def test_authorization_url_preserves_existing_query(self, http: httpx.AsyncClient) -> None:
        client = make_client(http, oauth_url=BUFFER_OAUTH_URL + "?x=1")
        assert "?x=1&client_id=cid" in client.authorization_url("s", code_verifier="v")

    def test_authorization_url_requires_state(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(ValueError, match="state"):
            make_client(http).authorization_url("", code_verifier="v")

    def test_not_configured(self, http: httpx.AsyncClient) -> None:
        client = make_client(http, client_secret=None)
        assert not client.configured
        with pytest.raises(ServiceNotConfiguredError):
            client.authorization_url("s", code_verifier="v")

    async def test_exchange_code(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        route = router.post(BUFFER_TOKEN_URL).respond(
            json={"access_token": "1/new", "expires_in": 3600, "refresh_token": "r"}
        )
        token = await make_client(http).exchange_code("the-code", code_verifier=VERIFIER)
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
            "redirect_uri": ["https://localhost:8000/buffer/callback"],
            "code": ["the-code"],
            "grant_type": ["authorization_code"],
            "code_verifier": [VERIFIER],
        }

    async def test_exchange_code_without_expiry(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(json={"access_token": "1/x"})
        token = await make_client(http).exchange_code("c", code_verifier="v")
        assert token.expires_at is None

    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_exchange_code_rejected(
        self, router: respx.MockRouter, http: httpx.AsyncClient, status: int
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(status, json={"error": "invalid_grant"})
        with pytest.raises(PublisherAuthError, match="authorization code"):
            await make_client(http).exchange_code("c", code_verifier="v")

    async def test_invalid_client_names_the_settings(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(401, json={"error": "invalid_client"})
        with pytest.raises(PublisherAuthError, match="BUFFER_CLIENT_ID"):
            await make_client(http).exchange_code("c", code_verifier="v")

    @pytest.mark.parametrize("payload", [{}, {"access_token": ""}, ["x"]])
    async def test_exchange_code_missing_token(
        self, router: respx.MockRouter, http: httpx.AsyncClient, payload: object
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(json=payload)
        with pytest.raises(PublisherAuthError, match="access token"):
            await make_client(http).exchange_code("c", code_verifier="v")

    async def test_exchange_code_is_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_TOKEN_URL).respond(503)
        with pytest.raises(PublisherError, match="temporarily unavailable"):
            await make_client(http).exchange_code("c", code_verifier="v")
        assert route.call_count == 1

    async def test_exchange_code_requires_code(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(PublisherAuthError, match="authorization code"):
            await make_client(http).exchange_code("", code_verifier="v")


class TestRefresh:
    async def test_rotates_tokens(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        route = router.post(BUFFER_TOKEN_URL).respond(
            json={"access_token": "2/new", "expires_in": 3600, "refresh_token": "r2"}
        )
        old = OAuthToken(access_token="1/old", refresh_token="r1")
        token = await make_client(http).refresh(old)
        assert (token.access_token, token.refresh_token) == ("2/new", "r2")
        assert form_body(route.calls.last.request) == {
            "client_id": ["cid"],
            "client_secret": ["csecret"],
            "grant_type": ["refresh_token"],
            "refresh_token": ["r1"],
        }

    async def test_keeps_refresh_token_when_not_rotated(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(json={"access_token": "2/new"})
        token = await make_client(http).refresh(OAuthToken(access_token="x", refresh_token="r1"))
        assert token.refresh_token == "r1"

    async def test_rejected_refresh_token(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        with pytest.raises(PublisherAuthError, match="expired"):
            await make_client(http).refresh(OAuthToken(access_token="x", refresh_token="r1"))

    async def test_needs_a_refresh_token(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(PublisherAuthError, match="expired"):
            await make_client(http).refresh(OAuthToken(access_token="x"))

    async def test_is_not_retried(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        route = router.post(BUFFER_TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(PublisherError, match="Could not reach"):
            await make_client(http).refresh(OAuthToken(access_token="x", refresh_token="r1"))
        assert route.call_count == 1  # a refresh token must never be sent twice


class TestProfiles:
    async def test_lists_channels_of_every_organization(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI(
            channels=[
                *CHANNELS_PAYLOAD,
                {"id": "gone", "service": "facebook", "isDisconnected": True},
                {"id": "over-plan", "service": "linkedin", "isLocked": True},
                {"service": "x"},
                "junk",
            ],
            organizations=("org-1", "org-2"),
        )
        router.post(BUFFER_API_URL).mock(side_effect=api)
        profiles = await make_client(http).list_profiles(TOKEN)
        expected = [("prof-ig", "instagram", "@acme"), ("prof-x", "twitter", "acme")]
        assert [(p.id, p.service, p.username) for p in profiles] == expected * 2
        assert profiles[0].avatar_url == "https://cdn.buffer.test/a.png"
        assert profiles[1].avatar_url is None
        channel_queries = [graphql_body(r) for r in api.requests[1:]]
        assert [q["variables"] for q in channel_queries] == [
            {"organizationId": "org-1"},
            {"organizationId": "org-2"},
        ]
        request = api.requests[0]
        assert request.headers["Authorization"] == "Bearer 1/abc"
        assert request.headers["Content-Type"] == "application/json"
        assert "access_token" not in str(request.url)

    async def test_channel_fallbacks(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI(channels=[{"id": 7, "avatar": "http://insecure/a.png"}])
        router.post(BUFFER_API_URL).mock(side_effect=api)
        (profile,) = await make_client(http).list_profiles(TOKEN)
        assert profile.id == "7"
        assert profile.service == "unknown"
        assert profile.username == "7"
        assert profile.avatar_url is None  # non-https avatars are dropped (mixed content)

    @pytest.mark.parametrize(
        "data",
        [{"account": None}, {"account": {"organizations": "nope"}}],
    )
    async def test_unexpected_account(
        self, router: respx.MockRouter, http: httpx.AsyncClient, data: object
    ) -> None:
        router.post(BUFFER_API_URL).respond(json={"data": data})
        with pytest.raises(PublisherAPIError, match="Unexpected account"):
            await make_client(http).list_profiles(TOKEN)

    async def test_unexpected_channels(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI()
        api.channels = "nope"  # type: ignore[assignment]
        router.post(BUFFER_API_URL).mock(side_effect=api)
        with pytest.raises(PublisherAPIError, match="Unexpected channel"):
            await make_client(http).list_profiles(TOKEN)

    async def test_retries_transient_errors(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI()
        limited = {"message": "slow down", "extensions": {"code": "RATE_LIMIT_EXCEEDED"}}
        responses: list[Any] = [
            httpx.Response(502),
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, json={"errors": [limited]}),
            api,  # organizations
            api,  # channels
        ]
        route = router.post(BUFFER_API_URL).mock(side_effect=responses)
        client = make_client(http, retry=RetryPolicy(max_attempts=4, jitter=0))
        assert len(await client.list_profiles(TOKEN)) == 2
        assert route.call_count == 5

    async def test_http_auth_failure(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_API_URL).respond(401, json={"error": "invalid token"})
        with pytest.raises(PublisherAuthError, match="connect Buffer again"):
            await make_client(http).list_profiles(TOKEN)

    @pytest.mark.parametrize("code", ["UNAUTHORIZED", "UNAUTHENTICATED"])
    async def test_graphql_auth_failure(
        self, router: respx.MockRouter, http: httpx.AsyncClient, code: str
    ) -> None:
        router.post(BUFFER_API_URL).respond(
            json={"errors": [{"message": "Not authorized", "extensions": {"code": code}}]}
        )
        with pytest.raises(PublisherAuthError, match="connect Buffer again"):
            await make_client(http).list_profiles(TOKEN)

    async def test_graphql_error_is_reported(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_API_URL).respond(
            json={"errors": [{"message": "No access", "extensions": {"code": "FORBIDDEN"}}]}
        )
        with pytest.raises(PublisherAPIError, match="No access"):
            await make_client(http).list_profiles(TOKEN)

    async def test_unexpected_server_error_is_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_API_URL).respond(
            json={"errors": ["junk-without-details", {"extensions": {"code": "UNEXPECTED"}}]}
        )
        with pytest.raises(PublisherAPIError, match="unknown error"):
            await make_client(http).list_profiles(TOKEN)
        assert route.call_count == 1  # first error has no code: not retryable

        route.respond(json={"errors": [{"message": "boom", "extensions": {"code": "UNEXPECTED"}}]})
        with pytest.raises(PublisherError, match="temporarily unavailable"):
            await make_client(http).list_profiles(TOKEN)
        assert route.call_count == 4

    @pytest.mark.parametrize("payload", [["x"], {"data": None}])
    async def test_invalid_payload(
        self, router: respx.MockRouter, http: httpx.AsyncClient, payload: object
    ) -> None:
        router.post(BUFFER_API_URL).respond(json=payload)
        with pytest.raises(PublisherAPIError, match="invalid response"):
            await make_client(http).list_profiles(TOKEN)

    async def test_timeout_after_retries(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
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
    async def test_schedule_creates_one_post_per_channel(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI()
        router.post(BUFFER_API_URL).mock(side_effect=api)
        result = await make_client(http).publish(TOKEN, schedule_post())
        assert result.update_ids == ("post-1", "post-2")
        assert result.failures == ()
        expected = {
            "text": "Hello world\n\n#a #b",
            "schedulingType": "automatic",
            "mode": "customScheduled",
            "dueAt": "2026-11-12T15:00:00Z",
            "assets": [{"image": {"url": "https://social.example.com/uploads/x.png"}}],
        }
        assert api.posts == [{"channelId": "p1", **expected}, {"channelId": "p2", **expected}]
        assert api.requests[0].headers["Authorization"] == "Bearer 1/abc"
        assert "mutation CreatePost" in graphql_body(api.requests[0])["query"]

    @pytest.mark.parametrize(
        ("mode", "share_mode"), [(PublishMode.NOW, "shareNow"), (PublishMode.QUEUE, "addToQueue")]
    )
    async def test_now_and_queue_modes(
        self,
        router: respx.MockRouter,
        http: httpx.AsyncClient,
        mode: PublishMode,
        share_mode: str,
    ) -> None:
        api = FakeBufferAPI()
        router.post(BUFFER_API_URL).mock(side_effect=api)
        post = schedule_post(profile_ids=("p1",), mode=mode, scheduled_at=None, media_url=None)
        result = await make_client(http).publish(TOKEN, post)
        assert result.update_ids == ("post-1",)
        (sent,) = api.posts
        assert sent["mode"] == share_mode
        assert "dueAt" not in sent
        assert "assets" not in sent

    async def test_partial_refusal_is_reported(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI()
        api.refusals["p1"] = "Text is too long for X"
        router.post(BUFFER_API_URL).mock(side_effect=api)
        result = await make_client(http).publish(TOKEN, schedule_post())
        assert result.update_ids == ("post-1",)
        assert result.failures == (("p1", "Buffer refused the post: Text is too long for X."),)

    async def test_refused_everywhere_raises(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI()
        api.refusals.update(p1="Queue is full", p2="Queue is full")
        router.post(BUFFER_API_URL).mock(side_effect=api)
        with pytest.raises(PublisherAPIError, match="Queue is full"):
            await make_client(http).publish(TOKEN, schedule_post())

    @pytest.mark.parametrize("result", [None, {"__typename": "UnexpectedError"}])
    async def test_malformed_mutation_result(
        self, router: respx.MockRouter, http: httpx.AsyncClient, result: object
    ) -> None:
        router.post(BUFFER_API_URL).respond(json={"data": {"createPost": result}})
        with pytest.raises(PublisherAPIError, match=r"invalid response|unknown error"):
            await make_client(http).publish(TOKEN, schedule_post(profile_ids=("p1",)))

    async def test_auth_error_stops_immediately(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_API_URL).respond(403)
        with pytest.raises(PublisherAuthError):
            await make_client(http).publish(TOKEN, schedule_post())
        assert route.call_count == 1

    async def test_server_error_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_API_URL).respond(500)
        with pytest.raises(PublisherError, match="temporarily unavailable"):
            await make_client(http).publish(TOKEN, schedule_post(profile_ids=("p1",)))
        assert route.call_count == 1

    async def test_read_timeout_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        route = router.post(BUFFER_API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(PublisherError, match="in time"):
            await make_client(http).publish(TOKEN, schedule_post(profile_ids=("p1",)))
        assert route.call_count == 1  # the post may have been created: never duplicate

    async def test_connect_error_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        api = FakeBufferAPI()
        responses: list[Any] = [httpx.ConnectError("refused"), api]
        route = router.post(BUFFER_API_URL).mock(side_effect=responses)
        result = await make_client(http).publish(TOKEN, schedule_post(profile_ids=("p1",)))
        assert result.update_ids == ("post-1",)
        assert route.call_count == 2

    async def test_http_error_detail(
        self, router: respx.MockRouter, http: httpx.AsyncClient
    ) -> None:
        router.post(BUFFER_API_URL).respond(404, text="not found")
        with pytest.raises(PublisherAPIError, match="HTTP 404"):
            await make_client(http).publish(TOKEN, schedule_post(profile_ids=("p1",)))

    async def test_invalid_json(self, router: respx.MockRouter, http: httpx.AsyncClient) -> None:
        router.post(BUFFER_API_URL).respond(200, text="ok")
        with pytest.raises(PublisherAPIError, match="invalid response"):
            await make_client(http).publish(TOKEN, schedule_post(profile_ids=("p1",)))


def test_format_buffer_datetime() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert format_buffer_datetime(datetime(2026, 1, 2, 17, 30, tzinfo=plus_two)) == (
        "2026-01-02T15:30:00Z"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        format_buffer_datetime(datetime(2026, 1, 2, 17, 30))


@pytest.mark.parametrize(
    ("url", "legacy"),
    [
        ("https://bufferapp.com/oauth2/authorize", True),
        ("https://api.bufferapp.com/1/oauth2/token.json", True),
        ("https://auth.buffer.com/auth", False),
        ("https://api.buffer.com", False),
        ("https://notbufferapp.com/x", False),
    ],
)
def test_is_legacy_url(url: str, legacy: bool) -> None:
    assert is_legacy_url(url) is legacy
