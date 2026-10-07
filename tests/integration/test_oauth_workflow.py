"""Buffer OAuth workflow: connect -> consent -> callback (state, PKCE) -> token -> channels."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from services.buffer import pkce_challenge
from services.publishing import OAuthToken
from tests.conftest import AppFactory
from tests.helpers import (
    BUFFER_API_URL,
    BUFFER_OAUTH_URL,
    BUFFER_TOKEN_URL,
    CHANNELS_PAYLOAD,
    AppClient,
    FakeBufferAPI,
    form_body,
    make_client,
    query_params,
    toast,
)

pytestmark = pytest.mark.integration


def flashes(html: str) -> list[dict[str, str]]:
    match = re.search(r'<script type="application/json" id="flash-data">(.*?)</script>', html)
    assert match
    data: list[dict[str, str]] = json.loads(match.group(1))
    return data


async def test_full_connect_flow(client: AppClient, mock_http: respx.MockRouter) -> None:
    token_route = mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "1/buffer-token"})
    api = FakeBufferAPI()
    mock_http.post(BUFFER_API_URL).mock(side_effect=api)

    start = await client.get("/buffer/auth")
    assert start.status_code == 303
    location = start.headers["location"]
    assert location.startswith(BUFFER_OAUTH_URL + "?")
    params = query_params(location)
    assert params["client_id"] == "client-id"
    assert params["redirect_uri"] == "http://testserver/buffer/callback"
    assert params["response_type"] == "code"
    assert params["scope"] == "account:read posts:write offline_access"
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 32

    callback = await client.get(
        "/buffer/callback", params={"code": "one-time-code", "state": params["state"]}
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    sent = form_body(token_route.calls.last.request)
    assert sent["code"] == ["one-time-code"]
    # PKCE: the verifier proves this server started the flow; the browser never saw it.
    verifier = sent["code_verifier"][0]
    assert pkce_challenge(verifier) == params["code_challenge"]
    assert verifier not in location
    assert verifier not in (client.http.cookies.get("mab_session") or "")

    page = (await client.get("/")).text
    assert "Buffer connected" in page
    assert "2 profiles" in page
    assert "@acme" in page
    assert "Instagram" in page
    assert flashes(page) == [
        {"level": "success", "message": "Buffer connected. You can now schedule posts."}
    ]
    assert api.requests[-1].headers["Authorization"] == "Bearer 1/buffer-token"

    # The token is stored encrypted server-side and never sent to the browser.
    assert "1/buffer-token" not in page
    assert "1/buffer-token" not in (client.http.cookies.get("mab_session") or "")
    async with client.container.database.conn.execute(
        "SELECT ciphertext FROM oauth_tokens"
    ) as cursor:
        (row,) = await cursor.fetchall()
    assert b"1/buffer-token" not in row["ciphertext"]

    # Flash messages are shown once.
    assert flashes((await client.get("/")).text) == []

    metrics = (await client.get("/metrics")).text
    assert 'mab_oauth_events_total{outcome="connected",provider="buffer"} 1.0' in metrics


async def test_forged_state_is_rejected(client: AppClient, mock_http: respx.MockRouter) -> None:
    token_route = mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "x"})
    await client.get("/buffer/auth")
    response = await client.get("/buffer/callback", params={"code": "c", "state": "forged"})
    assert response.status_code == 303
    assert token_route.call_count == 0
    page = (await client.get("/")).text
    assert "could not be verified" in flashes(page)[0]["message"]
    assert "Connect Buffer" in page


async def test_callback_without_prior_auth(client: AppClient, mock_http: respx.MockRouter) -> None:
    token_route = mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "x"})
    await client.get("/buffer/callback", params={"code": "c", "state": "anything"})
    assert token_route.call_count == 0


async def test_state_cannot_be_replayed(client: AppClient, mock_http: respx.MockRouter) -> None:
    token_route = mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "x"})
    mock_http.post(BUFFER_API_URL).mock(side_effect=FakeBufferAPI(channels=[]))
    start = await client.get("/buffer/auth")
    state = query_params(start.headers["location"])["state"]
    await client.get("/buffer/callback", params={"code": "c", "state": state})
    await client.get("/buffer/callback", params={"code": "c2", "state": state})
    assert token_route.call_count == 1


async def test_user_denied_access(client: AppClient) -> None:
    start = await client.get("/buffer/auth")
    state = query_params(start.headers["location"])["state"]
    await client.get(
        "/buffer/callback",
        params={"error": "access_denied", "error_description": "User said no", "state": state},
    )
    message = flashes((await client.get("/")).text)[0]
    assert message == {
        "level": "warning",
        "message": "Buffer authorization was not completed: User said no",
    }


async def test_missing_code(client: AppClient) -> None:
    start = await client.get("/buffer/auth")
    state = query_params(start.headers["location"])["state"]
    await client.get("/buffer/callback", params={"state": state})
    assert (
        "did not return an authorization code"
        in flashes((await client.get("/")).text)[0]["message"]
    )


async def test_code_exchange_failure(client: AppClient, mock_http: respx.MockRouter) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    response = await client.connect_buffer()
    assert response.status_code == 303
    page = (await client.get("/")).text
    assert "rejected the authorization code" in flashes(page)[0]["message"]
    assert "Connect Buffer" in page


async def test_invalid_client_points_at_configuration(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(401, json={"error": "invalid_client"})
    await client.connect_buffer()
    message = flashes((await client.get("/")).text)[0]["message"]
    assert "invalid_client" in message
    assert "BUFFER_CLIENT_ID" in message


async def test_error_code_shown_without_description(client: AppClient) -> None:
    start = await client.get("/buffer/auth")
    state = query_params(start.headers["location"])["state"]
    await client.get("/buffer/callback", params={"error": "invalid_scope", "state": state})
    message = flashes((await client.get("/")).text)[0]["message"]
    assert message == "Buffer authorization was not completed: invalid_scope"


async def test_expired_token_is_refreshed_transparently(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    token_route = mock_http.post(BUFFER_TOKEN_URL).respond(
        json={"access_token": "2/renewed", "expires_in": 3600, "refresh_token": "r2"}
    )
    api = FakeBufferAPI()
    mock_http.post(BUFFER_API_URL).mock(side_effect=api)
    stale = OAuthToken(
        access_token="1/stale",
        refresh_token="r1",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await client.container.tokens.save(client.session_id, "buffer", stale)

    page = (await client.get("/")).text
    assert "2 profiles" in page
    assert form_body(token_route.calls.last.request)["refresh_token"] == ["r1"]
    assert api.requests[-1].headers["Authorization"] == "Bearer 2/renewed"
    stored = await client.container.tokens.get(client.session_id, "buffer")
    assert stored is not None
    assert (stored.access_token, stored.refresh_token) == ("2/renewed", "r2")


async def test_not_configured(app_factory: AppFactory) -> None:
    async with make_client(app_factory(buffer_client_secret=None)) as client:
        response = await client.get("/buffer/auth")
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        page = (await client.get("/")).text
        assert "Buffer not configured" in page
        assert flashes(page)[0]["message"] == "Buffer is not configured on this server."


async def test_disconnect(client: AppClient, mock_http: respx.MockRouter) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "t"})
    mock_http.post(BUFFER_API_URL).mock(side_effect=FakeBufferAPI())
    await client.connect_buffer()
    response = await client.post("/buffer/disconnect")
    assert response.status_code == 200
    assert response.headers["HX-Refresh"] == "true"
    page = (await client.get("/")).text
    assert "Connect Buffer" in page
    assert {"level": "info", "message": "Buffer disconnected."} in flashes(page)


async def test_refresh_profiles(client: AppClient, mock_http: respx.MockRouter) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "t"})
    api = FakeBufferAPI()
    route = mock_http.post(BUFFER_API_URL).mock(side_effect=api)
    await client.connect_buffer()
    await client.get("/")  # cached after this
    calls = route.call_count
    await client.get("/")
    assert route.call_count == calls

    api.channels = CHANNELS_PAYLOAD[:1]
    refreshed = await client.get("/buffer/refresh")
    assert refreshed.status_code == 303
    page = (await client.get("/")).text
    assert "Found 1 Buffer profile(s)." in [f["message"] for f in flashes(page)]
    assert route.call_count == calls + 2  # organizations + channels

    route.side_effect = lambda request: httpx.Response(500)

    await client.get("/buffer/refresh")
    page = (await client.get("/")).text
    assert any("temporarily unavailable" in f["message"] for f in flashes(page))


async def test_revoked_token_shows_connect_again(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "t"})
    mock_http.post(BUFFER_API_URL).respond(401)
    await client.connect_buffer()
    page = (await client.get("/")).text
    assert "Connect Buffer" in page
    assert "rejected the access token" in page
    assert await client.container.accounts.token(client.session_id) is None


async def test_auth_endpoints_are_rate_limited(app_factory: AppFactory) -> None:
    app = app_factory(rate_limit_enabled=True, rate_limit_auth="2/minute")
    with respx.mock(assert_all_mocked=True):
        async with make_client(app) as client:
            assert (await client.get("/buffer/auth")).status_code == 303
            assert (await client.get("/buffer/auth")).status_code == 303
            limited = await client.get("/buffer/auth")
            assert limited.status_code == 429
            assert "Too many requests" in limited.text
            htmx = await client.post("/buffer/disconnect")
            assert htmx.status_code == 429
            assert toast(htmx)["level"] == "danger"
