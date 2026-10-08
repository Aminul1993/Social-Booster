"""Buffer connection: personal access token -> channels, refresh, rejection, outages."""

from __future__ import annotations

import json
import re

import pytest
import respx
from fastapi import FastAPI

from tests.conftest import AppFactory
from tests.helpers import (
    BUFFER_ACCESS_TOKEN,
    BUFFER_API_URL,
    CHANNELS_PAYLOAD,
    AppClient,
    FakeBufferAPI,
    make_client,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def buffer_api(mock_http: respx.MockRouter) -> FakeBufferAPI:
    """Buffer's GraphQL endpoint, stubbed before the client's first page load."""
    api = FakeBufferAPI()
    mock_http.post(BUFFER_API_URL).mock(side_effect=api)
    return api


@pytest.fixture
def app(app_factory: AppFactory, buffer_api: FakeBufferAPI) -> FastAPI:
    """Buffer configured with an access token (tests default to unconfigured)."""
    return app_factory(buffer_access_token=BUFFER_ACCESS_TOKEN)


def flashes(html: str) -> list[dict[str, str]]:
    match = re.search(r'<script type="application/json" id="flash-data">(.*?)</script>', html)
    assert match
    data: list[dict[str, str]] = json.loads(match.group(1))
    return data


async def test_configured_token_lists_profiles(
    client: AppClient, buffer_api: FakeBufferAPI
) -> None:
    page = (await client.get("/")).text
    assert "Buffer connected" in page
    assert "2 profiles" in page
    assert "@acme" in page
    assert "Instagram" in page
    assert "/buffer/auth" not in page  # nothing to connect: the server holds the key
    assert buffer_api.requests[-1].headers["Authorization"] == f"Bearer {BUFFER_ACCESS_TOKEN}"

    # The key never reaches the browser.
    assert BUFFER_ACCESS_TOKEN not in page
    assert BUFFER_ACCESS_TOKEN not in (client.http.cookies.get("mab_session") or "")


async def test_profiles_are_shared_across_sessions(
    client: AppClient, buffer_api: FakeBufferAPI
) -> None:
    assert len(buffer_api.requests) == 2  # organizations + channels, on the first page load

    client.http.cookies.clear()  # a different visitor
    page = (await client.get("/")).text
    assert "2 profiles" in page
    assert len(buffer_api.requests) == 2


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/buffer/auth"), ("GET", "/buffer/callback"), ("POST", "/buffer/disconnect")],
)
async def test_oauth_routes_are_gone(client: AppClient, method: str, path: str) -> None:
    response = await client.http.request(method, path, headers=client.htmx_headers())
    assert response.status_code == 404


async def test_not_configured(app_factory: AppFactory) -> None:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        route = router.post(BUFFER_API_URL).mock(side_effect=FakeBufferAPI())
        async with make_client(app_factory(buffer_access_token=None)) as client:
            assert (await client.get("/buffer/refresh")).status_code == 303
            page = (await client.get("/")).text
    assert "Buffer not configured" in page
    assert 'title="Set BUFFER_ACCESS_TOKEN"' in page
    assert flashes(page) == []
    assert route.call_count == 0


async def test_refresh_profiles(
    client: AppClient, buffer_api: FakeBufferAPI, mock_http: respx.MockRouter
) -> None:
    calls = len(buffer_api.requests)
    await client.get("/")  # cached
    assert len(buffer_api.requests) == calls

    buffer_api.channels = CHANNELS_PAYLOAD[:1]
    refreshed = await client.get("/buffer/refresh")
    assert refreshed.status_code == 303
    assert refreshed.headers["location"] == "/"
    page = (await client.get("/")).text
    assert "Found 1 Buffer profile(s)." in [f["message"] for f in flashes(page)]
    assert len(buffer_api.requests) == calls + 2  # organizations + channels

    mock_http.post(BUFFER_API_URL).respond(500)
    await client.get("/buffer/refresh")
    page = (await client.get("/")).text
    assert any("temporarily unavailable" in f["message"] for f in flashes(page))


async def test_rejected_token_is_shown(app_factory: AppFactory) -> None:
    app = app_factory(buffer_access_token="revoked-key")
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(BUFFER_API_URL).respond(401)
        async with make_client(app) as client:
            page = (await client.get("/")).text
            assert "rejected the configured access token" in page
            assert "Buffer unavailable" in page
            assert 'href="/buffer/refresh"' in page

            await client.get("/buffer/refresh")
            message = flashes((await client.get("/")).text)[0]
            assert message["level"] == "danger"
            assert "BUFFER_ACCESS_TOKEN" in message["message"]

            # Once the key works again (e.g. re-enabled in Buffer), a retry recovers.
            route.side_effect = FakeBufferAPI()
            await client.get("/buffer/refresh")
            page = (await client.get("/")).text
    assert "Buffer connected" in page
    assert "Found 2 Buffer profile(s)." in [f["message"] for f in flashes(page)]


async def test_refresh_is_rate_limited(app_factory: AppFactory) -> None:
    app = app_factory(
        buffer_access_token=BUFFER_ACCESS_TOKEN,
        rate_limit_enabled=True,
        rate_limit_default="2/minute",
    )
    with respx.mock(assert_all_mocked=True) as router:
        api = FakeBufferAPI()
        router.post(BUFFER_API_URL).mock(side_effect=api)
        async with make_client(app) as client:
            assert (await client.get("/buffer/refresh")).status_code == 303
            assert (await client.get("/buffer/refresh")).status_code == 303
            calls = len(api.requests)
            limited = await client.get("/buffer/refresh")
    assert limited.status_code == 429
    assert "Too many requests" in limited.text
    assert len(api.requests) == calls  # Buffer was not called
