"""Buffer OAuth workflow: connect -> consent -> callback (state check) -> token -> profiles."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from tests.conftest import AppFactory
from tests.helpers import (
    BUFFER_OAUTH_URL,
    BUFFER_PROFILES_URL,
    BUFFER_TOKEN_URL,
    PROFILES_PAYLOAD,
    AppClient,
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
    profiles_route = mock_http.get(BUFFER_PROFILES_URL).respond(json=PROFILES_PAYLOAD)

    start = await client.get("/buffer/auth")
    assert start.status_code == 303
    location = start.headers["location"]
    assert location.startswith(BUFFER_OAUTH_URL + "?")
    params = query_params(location)
    assert params["client_id"] == "client-id"
    assert params["redirect_uri"] == "http://testserver/buffer/callback"
    assert params["response_type"] == "code"
    assert len(params["state"]) >= 32

    callback = await client.get(
        "/buffer/callback", params={"code": "one-time-code", "state": params["state"]}
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert form_body(token_route.calls.last.request)["code"] == ["one-time-code"]

    page = (await client.get("/")).text
    assert "Buffer connected" in page
    assert "2 profiles" in page
    assert "@acme" in page
    assert "Instagram" in page
    assert flashes(page) == [
        {"level": "success", "message": "Buffer connected. You can now schedule posts."}
    ]
    assert profiles_route.calls.last.request.headers["Authorization"] == "Bearer 1/buffer-token"

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
    mock_http.get(BUFFER_PROFILES_URL).respond(json=[])
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
    mock_http.get(BUFFER_PROFILES_URL).respond(json=PROFILES_PAYLOAD)
    await client.connect_buffer()
    response = await client.post("/buffer/disconnect")
    assert response.status_code == 200
    assert response.headers["HX-Refresh"] == "true"
    page = (await client.get("/")).text
    assert "Connect Buffer" in page
    assert {"level": "info", "message": "Buffer disconnected."} in flashes(page)


async def test_refresh_profiles(client: AppClient, mock_http: respx.MockRouter) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "t"})
    profiles = mock_http.get(BUFFER_PROFILES_URL).mock(
        side_effect=[
            httpx.Response(200, json=PROFILES_PAYLOAD),
            httpx.Response(200, json=PROFILES_PAYLOAD[:1]),
            httpx.Response(500),
        ]
    )
    await client.connect_buffer()
    await client.get("/")  # cached after this
    refreshed = await client.get("/buffer/refresh")
    assert refreshed.status_code == 303
    page = (await client.get("/")).text
    assert "Found 1 Buffer profile(s)." in [f["message"] for f in flashes(page)]
    assert profiles.call_count == 2

    await client.get("/buffer/refresh")
    page = (await client.get("/")).text
    assert any("temporarily unavailable" in f["message"] for f in flashes(page))


async def test_revoked_token_shows_connect_again(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(BUFFER_TOKEN_URL).respond(json={"access_token": "t"})
    mock_http.get(BUFFER_PROFILES_URL).respond(401)
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
