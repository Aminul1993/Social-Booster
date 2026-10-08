"""AI generation workflow: card -> /generate -> Ollama Cloud -> edited copy -> autosave."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.models import DraftStatus, PublishRecord
from services.publishing import PublishMode
from tests.conftest import AppFactory
from tests.helpers import (
    GOOD_COPY,
    OLLAMA_URL,
    AppClient,
    make_client,
    ollama_reply,
    toast,
)

pytestmark = pytest.mark.integration


async def test_generate_caption_and_hashtags(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(OLLAMA_URL).respond(json=ollama_reply(GOOD_COPY))
    draft_id = await client.upload_one()

    response = await client.post(
        "/generate",
        data={"draft_id": draft_id, "keywords": "golden retriever, beach", "tone": "playful"},
    )

    assert response.status_code == 200, response.text
    assert toast(response)["level"] == "success"
    html = response.text
    assert f'id="draft-{draft_id}"' in html
    assert "Fetch mode: activated. Pure joy on four paws.</textarea>" in html
    assert 'value="#dogsofinstagram #goldenretriever #puppylove #doglife #fetch"' in html
    assert "autofocus" in html
    assert "Regenerate copy" in html
    assert "Written by test-model" in html
    assert '<option value="playful" selected>' in html
    assert "Buffer is not configured on this server" in html  # no BUFFER_ACCESS_TOKEN

    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "test-model"
    assert '["golden retriever", "beach"]' in sent["messages"][1]["content"]
    assert "Tone of voice: playful" in sent["messages"][1]["content"]
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-ollama-key"

    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.status is DraftStatus.GENERATED
    assert draft.keywords == ["golden retriever", "beach"]
    assert draft.hashtags[0] == "#dogsofinstagram"

    metrics = (await client.get("/metrics")).text
    assert 'mab_ai_generations_total{outcome="success"} 1.0' in metrics


async def test_keywords_required_renders_inline_error(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(OLLAMA_URL).respond(json=ollama_reply(GOOD_COPY))
    draft_id = await client.upload_one()
    response = await client.post("/generate", data={"draft_id": draft_id, "keywords": " ,;, "})
    assert response.status_code == 422
    assert "is-invalid" in response.text
    assert "Add at least one keyword describing the image." in response.text
    assert route.call_count == 0


async def test_ollama_auth_failure_keeps_card(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(OLLAMA_URL).respond(401)
    draft_id = await client.upload_one()
    response = await client.post("/generate", data={"draft_id": draft_id, "keywords": "dog"})
    assert response.status_code == 502
    assert response.headers["HX-Reswap"] == "none"
    assert "OLLAMA_API_KEY" in toast(response)["message"]
    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.caption == ""
    metrics = (await client.get("/metrics")).text
    assert 'mab_ai_generations_total{outcome="error"} 1.0' in metrics


async def test_ollama_outage_after_retries(app_factory: AppFactory) -> None:
    app = app_factory(ollama_max_retries=1)
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(OLLAMA_URL).respond(503)
        async with make_client(app) as client:
            draft_id = await client.upload_one()
            response = await client.post("/generate", data={"draft_id": draft_id, "keywords": "x"})
    assert response.status_code == 503
    assert "busy" in toast(response)["message"]
    assert route.call_count == 1


async def test_ollama_not_configured(app_factory: AppFactory) -> None:
    app = app_factory(ollama_api_key=None, ollama_endpoint="https://ollama.com/v1/chat/completions")
    with respx.mock(assert_all_mocked=True):
        async with make_client(app) as client:
            draft_id = await client.upload_one()
            response = await client.post("/generate", data={"draft_id": draft_id, "keywords": "x"})
    assert response.status_code == 503
    assert "not configured" in toast(response)["message"]


async def test_unknown_draft(client: AppClient) -> None:
    response = await client.post("/generate", data={"draft_id": "a" * 32, "keywords": "x"})
    assert response.status_code == 404


async def test_autosave_edits(client: AppClient, mock_http: respx.MockRouter) -> None:
    mock_http.post(OLLAMA_URL).respond(json=ollama_reply(GOOD_COPY))
    draft_id = await client.upload_one()
    await client.post("/generate", data={"draft_id": draft_id, "keywords": "dog"})

    saved = await client.patch(
        f"/drafts/{draft_id}",
        data={
            "caption": "  My own caption  ",
            "hashtags": "dogs, #Dogs puppy",
            "draft_id": draft_id,
        },
    )
    assert saved.status_code == 200
    assert "Saved" in saved.text
    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.caption == "My own caption"
    assert draft.hashtags == ["#dogs", "#puppy"]

    unchanged = await client.patch(
        f"/drafts/{draft_id}", data={"caption": "My own caption", "hashtags": "#dogs #puppy"}
    )
    assert unchanged.status_code == 200

    invalid = await client.patch(f"/drafts/{draft_id}", data={"caption": "x" * 2300})
    assert invalid.status_code == 422
    assert "Not saved" in invalid.text
    draft = await client.container.drafts.get(client.session_id, draft_id)
    assert draft.caption == "My own caption"

    # The page reflects the edit after reload.
    assert "My own caption</textarea>" in (await client.get("/")).text


async def test_regenerate_after_publish_keeps_published_status(
    client: AppClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(OLLAMA_URL).mock(
        side_effect=[
            httpx.Response(200, json=ollama_reply(GOOD_COPY)),
            httpx.Response(200, json=ollama_reply("CAPTION: Second take\nHASHTAGS: #two")),
        ]
    )
    draft_id = await client.upload_one()
    await client.post("/generate", data={"draft_id": draft_id, "keywords": "dog"})
    drafts = client.container.drafts
    draft = await drafts.get(client.session_id, draft_id)
    await drafts.record_publish(
        draft, PublishRecord(provider="buffer", mode=PublishMode.NOW, profile_ids=["p"])
    )

    response = await client.post("/generate", data={"draft_id": draft_id, "keywords": "dog"})
    assert response.status_code == 200
    assert "Second take</textarea>" in response.text
    assert 'hx-confirm="Replace the current caption' in response.text
    updated = await drafts.get(client.session_id, draft_id)
    assert updated.status is DraftStatus.PUBLISHED
    assert updated.hashtags == ["#two", "#dog"]  # topped up from the keywords
    assert "Sent to Buffer" in response.text
