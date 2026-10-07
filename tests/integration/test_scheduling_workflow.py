"""Scheduling workflow: upload -> generate -> connect Buffer -> edit -> schedule."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.models import DraftStatus
from services.publishing import PublishMode
from tests.conftest import AppFactory
from tests.helpers import (
    BUFFER_API_URL,
    BUFFER_TOKEN_URL,
    GOOD_COPY,
    OLLAMA_URL,
    AppClient,
    FakeBufferAPI,
    make_client,
    ollama_reply,
    toast,
)

pytestmark = pytest.mark.integration


def future_local(hours: int = 48) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def mock_services(router: respx.MockRouter) -> FakeBufferAPI:
    router.post(OLLAMA_URL).respond(json=ollama_reply(GOOD_COPY))
    router.post(BUFFER_TOKEN_URL).respond(json={"access_token": "1/tok"})
    api = FakeBufferAPI()
    router.post(BUFFER_API_URL).mock(side_effect=api)
    return api


async def prepare(client: AppClient, *, connect: bool = True) -> str:
    draft_id = await client.upload_one()
    generated = await client.post("/generate", data={"draft_id": draft_id, "keywords": "dog"})
    assert generated.status_code == 200
    if connect:
        await client.connect_buffer()
    return draft_id


def schedule_form(draft_id: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "draft_id": draft_id,
        "keywords": "dog",
        "tone": "friendly",
        "caption": "Edited caption for Buffer",
        "hashtags": "#dogs puppy",
        "profile_ids": ["prof-ig", "prof-x"],
        "mode": "schedule",
        "scheduled_for": future_local(),
        "timezone": "Asia/Dhaka",
    }
    values.update(overrides)
    return values


def image_url(post: dict[str, Any]) -> str:
    url: str = post["assets"][0]["image"]["url"]
    return url


async def test_schedule_end_to_end(client: AppClient, mock_http: respx.MockRouter) -> None:
    api = mock_services(mock_http)
    draft_id = await prepare(client)

    # The generated card now offers profile selection.
    card = (await client.get("/")).text
    assert 'name="profile_ids" value="prof-ig"' in card
    assert 'name="mode" id=' in card

    when = future_local(72)
    response = await client.post(
        "/buffer/schedule", data=schedule_form(draft_id, scheduled_for=when)
    )
    assert response.status_code == 200, response.text
    assert toast(response)["level"] == "success"
    assert "Scheduled on 2 profiles" in toast(response)["message"]
    html = response.text
    assert "Sent to Buffer" in html
    assert "scheduled for" in html
    assert "@acme, acme" in html

    # One createPost mutation per channel, with the same content.
    assert [post["channelId"] for post in api.posts] == ["prof-ig", "prof-x"]
    expected_utc = datetime.fromisoformat(when).replace(tzinfo=_tz("Asia/Dhaka")).astimezone(UTC)
    for post in api.posts:
        assert post["text"] == "Edited caption for Buffer\n\n#dogs #puppy"
        assert post["mode"] == "customScheduled"
        assert post["schedulingType"] == "automatic"
        assert post["dueAt"] == expected_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert image_url(post).startswith("https://social.example.com/uploads/")
        assert image_url(post).endswith(".png")
    mutation = api.requests[-1]
    assert "createPost" in json.loads(mutation.content)["query"]
    assert mutation.headers["Authorization"] == "Bearer 1/tok"

    assert "already sent to Buffer. Send it again?" in html  # guards accidental re-posts

    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.status is DraftStatus.PUBLISHED
    assert draft.caption == "Edited caption for Buffer"
    assert draft.hashtags == ["#dogs", "#puppy"]
    assert draft.publish is not None
    assert draft.publish.update_ids == ["post-1", "post-2"]
    assert draft.publish.scheduled_at == expected_utc
    assert draft.publish.profile_names == ["@acme", "acme"]

    metrics = (await client.get("/metrics")).text
    assert (
        'mab_publish_requests_total{mode="schedule",outcome="success",provider="buffer"} 1.0'
        in metrics
    )


@pytest.mark.parametrize(
    ("mode", "message", "share_mode"),
    [
        (PublishMode.QUEUE, "Added to the Buffer queue", "addToQueue"),
        (PublishMode.NOW, "Shared now", "shareNow"),
    ],
)
async def test_queue_and_now_modes(
    client: AppClient,
    mock_http: respx.MockRouter,
    mode: PublishMode,
    message: str,
    share_mode: str,
) -> None:
    api = mock_services(mock_http)
    draft_id = await prepare(client)
    response = await client.post(
        "/buffer/schedule",
        data=schedule_form(draft_id, mode=mode.value, scheduled_for="", profile_ids=["prof-ig"]),
    )
    assert response.status_code == 200
    assert message in toast(response)["message"]
    (post,) = api.posts
    assert post["mode"] == share_mode
    assert "dueAt" not in post


async def test_partial_refusal_is_reported(client: AppClient, mock_http: respx.MockRouter) -> None:
    api = mock_services(mock_http)
    api.refusals["prof-x"] = "Text is too long for X"
    draft_id = await prepare(client)
    response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 200
    message = toast(response)
    assert message["level"] == "warning"
    assert "Scheduled on 1 profile " in message["message"]
    assert (
        "Not sent to acme: Buffer refused the post: Text is too long for X." in message["message"]
    )
    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.publish is not None
    assert draft.publish.profile_ids == ["prof-ig"]
    assert draft.publish.profile_names == ["@acme"]


async def test_validation_errors_render_inline(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    api = mock_services(mock_http)
    draft_id = await prepare(client)
    response = await client.post(
        "/buffer/schedule",
        data=schedule_form(
            draft_id,
            caption="Keep my text",
            profile_ids=[],
            scheduled_for="2001-01-01T10:00",
        ),
    )
    assert response.status_code == 422
    assert toast(response)["message"] == "Please fix the highlighted fields."
    html = response.text
    assert "Choose at least one profile." in html
    assert "at least one minute in the future" in html
    assert "Keep my text</textarea>" in html  # user input preserved
    assert 'value="2001-01-01T10:00"' in html
    assert api.post_attempts == 0


async def test_unknown_profile_rejected(client: AppClient, mock_http: respx.MockRouter) -> None:
    api = mock_services(mock_http)
    draft_id = await prepare(client)
    response = await client.post(
        "/buffer/schedule", data=schedule_form(draft_id, profile_ids=["someone-else"])
    )
    assert response.status_code == 422
    assert "no longer available" in response.text
    assert api.post_attempts == 0


async def test_requires_connection(client: AppClient, mock_http: respx.MockRouter) -> None:
    mock_services(mock_http)
    draft_id = await prepare(client, connect=False)
    response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 401
    assert toast(response)["message"] == "Connect your Buffer account first."


async def test_requires_configuration(app_factory: AppFactory) -> None:
    app = app_factory(buffer_client_id=None)
    with respx.mock(assert_all_called=False) as router:
        mock_services(router)
        async with make_client(app) as client:
            draft_id = await prepare(client, connect=False)
            response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 401
    assert "not configured" in toast(response)["message"]


async def test_buffer_rejects_post(client: AppClient, mock_http: respx.MockRouter) -> None:
    api = mock_services(mock_http)
    api.refusals.update({"prof-ig": "Text too long", "prof-x": "Text too long"})
    draft_id = await prepare(client)
    response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 502
    assert response.headers["HX-Reswap"] == "none"
    assert "Text too long" in toast(response)["message"]
    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.status is DraftStatus.GENERATED
    metrics = (await client.get("/metrics")).text
    assert 'outcome="error"' in metrics


async def test_revoked_token_during_publish(client: AppClient, mock_http: respx.MockRouter) -> None:
    api = mock_services(mock_http)

    def revoked_on_post(request: httpx.Request) -> httpx.Response:
        if b"createPost" in request.content:
            return httpx.Response(401)
        return api(request)

    mock_http.post(BUFFER_API_URL).mock(side_effect=revoked_on_post)
    draft_id = await prepare(client)
    response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 401
    assert response.headers["HX-Refresh"] == "true"
    assert await client.container.accounts.token(client.session_id) is None
    page = (await client.get("/")).text
    assert "rejected the access token" in page


async def test_buffer_outage_while_loading_profiles(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    mock_services(mock_http)
    draft_id = await prepare(client)
    client.container.accounts._cache.clear()
    mock_http.post(BUFFER_API_URL).mock(side_effect=httpx.ConnectError("down"))
    response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 503
    assert "Could not reach Buffer" in toast(response)["message"]


async def test_local_image_url_warning(app_factory: AppFactory) -> None:
    app = app_factory(public_base_url=None)
    with respx.mock(assert_all_called=False) as router:
        api = mock_services(router)
        async with make_client(app) as client:
            draft_id = await prepare(client)
            response = await client.post("/buffer/schedule", data=schedule_form(draft_id))
    assert response.status_code == 200
    assert toast(response)["level"] == "warning"
    assert "PUBLIC_BASE_URL" in toast(response)["message"]
    assert image_url(api.posts[0]).startswith("http://testserver/uploads/")


def _tz(name: str) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)
