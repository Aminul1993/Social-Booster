from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx

from services.errors import OllamaAuthError, OllamaError, OllamaResponseError
from services.ollama import (
    OllamaClient,
    OllamaConfig,
    ParsedCopy,
    finalize_copy,
    parse_copy,
)
from services.prompts import CopyRequest, Tone
from services.retry import RetryPolicy
from tests.helpers import GOOD_COPY, OLLAMA_URL, ollama_reply

REQUEST = CopyRequest(keywords=("golden retriever", "tennis ball"), tone=Tone.FRIENDLY)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def make_client(http: httpx.AsyncClient, sleeps: Sleeps, **config: object) -> OllamaClient:
    values: dict[str, object] = {
        "endpoint": OLLAMA_URL,
        "model": "test-model",
        "api_key": "secret-key",
        "retry": RetryPolicy(max_attempts=3, jitter=0),
    }
    values.update(config)
    return OllamaClient(OllamaConfig(**values), http, sleep=sleeps)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- config
class TestConfig:
    def test_api_style_detection(self) -> None:
        assert OllamaConfig(endpoint="https://ollama.com/v1/chat/completions").api_style == "openai"
        assert OllamaConfig(endpoint="http://localhost:11434/api/chat/").api_style == "native"

    def test_configured_rules(self) -> None:
        assert not OllamaConfig(endpoint="https://ollama.com/v1/chat/completions").configured
        assert OllamaConfig(endpoint="https://api.ollama.com/x", api_key="k").configured
        assert OllamaConfig(endpoint="http://localhost:11434/api/chat").configured
        assert not OllamaConfig(endpoint="", api_key="k").configured


# -------------------------------------------------------------------------- parsing
class TestParsing:
    def test_json(self) -> None:
        parsed = parse_copy(GOOD_COPY)
        assert parsed.strategy == "json"
        assert parsed.caption.startswith("Fetch mode")
        assert parsed.hashtags[0] == "#dogsofinstagram"

    def test_fenced_json_with_reasoning_block(self) -> None:
        raw = (
            "<think>The user wants JSON.</think>\n```json\n"
            '{"Caption": "\\"Hello\\"", "Tags": "#a #b"}\n```'
        )
        parsed = parse_copy(raw)
        assert parsed == ParsedCopy(caption="Hello", hashtags=["#a", "#b"], strategy="json")

    def test_json_with_unusable_hashtags_type(self) -> None:
        parsed = parse_copy('{"caption": "Hi", "hashtags": 42}')
        assert parsed.hashtags == []

    def test_labeled_lines_like_the_original_prompt(self) -> None:
        raw = "**CAPTION:** Sun, sand and a good book.\nHASHTAGS: #beach #summer, reading"
        parsed = parse_copy(raw)
        assert parsed.strategy == "labeled"
        assert parsed.caption == "Sun, sand and a good book."
        assert parsed.hashtags == ["#beach", "#summer", "#reading"]

    def test_labeled_caption_without_hashtags(self) -> None:
        parsed = parse_copy("Caption - Just a caption")
        assert parsed.hashtags == []

    def test_heuristic_free_text(self) -> None:
        raw = "Here you go:\n#coffee #morning\n"
        parsed = parse_copy(raw)
        assert parsed.strategy == "heuristic"
        assert parsed.caption == "Here you go:"
        assert parsed.hashtags == ["#coffee", "#morning"]

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "#only #tags", "<think>nothing</think>", '{"caption": ""}'],
    )
    def test_unparseable(self, raw: str) -> None:
        with pytest.raises(OllamaResponseError):
            parse_copy(raw)

    def test_json_not_object_falls_back(self) -> None:
        assert parse_copy('["a"] and text').strategy == "heuristic"
        assert parse_copy("{not json} hello").strategy == "heuristic"


class TestFinalize:
    def test_truncates_caption_and_caps_hashtags(self) -> None:
        parsed = ParsedCopy(
            caption="word " * 100, hashtags=[f"#t{i}" for i in range(12)], strategy="json"
        )
        result = finalize_copy(parsed, REQUEST, model="m")
        assert len(result.caption) <= REQUEST.caption_max_chars
        assert len(result.hashtags) == REQUEST.max_hashtags

    def test_tops_up_hashtags_from_keywords(self) -> None:
        parsed = ParsedCopy(caption="Hi", hashtags=["#tennisball"], strategy="json")
        result = finalize_copy(parsed, REQUEST, model="m")
        assert result.hashtags == ("#tennisball", "#goldenretriever")

    def test_empty_caption_after_normalisation(self) -> None:
        parsed = ParsedCopy(caption="\x00\x01", hashtags=[], strategy="json")
        with pytest.raises(OllamaResponseError, match="empty caption"):
            finalize_copy(parsed, REQUEST, model="m")


# --------------------------------------------------------------------------- client
@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock_router:
        yield mock_router


class TestClient:
    async def test_generate_copy_openai_payload(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        route = router.post(OLLAMA_URL).respond(json=ollama_reply(GOOD_COPY))
        copy = await make_client(http, sleeps).generate_copy(REQUEST)

        assert copy.caption == "Fetch mode: activated. Pure joy on four paws."
        assert copy.hashtags[:2] == ("#dogsofinstagram", "#goldenretriever")
        assert copy.model == "test-model"
        assert not copy.repaired
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer secret-key"
        body = json.loads(sent.content)
        assert body["model"] == "test-model"
        assert body["max_tokens"] == 512
        assert body["stream"] is False
        assert [m["role"] for m in body["messages"]] == ["system", "user"]

    async def test_native_api_payload_and_response(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        url = "http://localhost:11434/api/chat"
        route = router.post(url).respond(
            json={"message": {"role": "assistant", "content": GOOD_COPY}, "done": True}
        )
        client = make_client(http, sleeps, endpoint=url, api_key=None)
        copy = await client.generate_copy(REQUEST)
        assert copy.caption
        sent = route.calls.last.request
        assert "Authorization" not in sent.headers
        body = json.loads(sent.content)
        assert body["options"] == {"temperature": 0.7, "num_predict": 512}

    async def test_retries_on_503_honouring_retry_after(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        route = router.post(OLLAMA_URL).mock(
            side_effect=[
                httpx.Response(503, headers={"Retry-After": "2"}),
                httpx.Response(429),
                httpx.Response(200, json=ollama_reply(GOOD_COPY)),
            ]
        )
        copy = await make_client(http, sleeps).generate_copy(REQUEST)
        assert copy.caption
        assert route.call_count == 3
        assert sleeps.delays == [2.0, 1.0]

    async def test_retries_exhausted(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        router.post(OLLAMA_URL).respond(502)
        with pytest.raises(OllamaError, match="busy") as excinfo:
            await make_client(http, sleeps).chat([{"role": "user", "content": "x"}])
        assert excinfo.value.retryable
        assert len(sleeps.delays) == 2

    async def test_timeout_and_connection_errors_retry(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        route = router.post(OLLAMA_URL).mock(
            side_effect=[
                httpx.ReadTimeout("slow"),
                httpx.ConnectError("refused"),
                httpx.Response(200, json=ollama_reply("CAPTION: Hi\nHASHTAGS: #a")),
            ]
        )
        assert await make_client(http, sleeps).chat([{"role": "user", "content": "x"}])
        assert route.call_count == 3

    async def test_timeout_message(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        router.post(OLLAMA_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(OllamaError, match="timed out"):
            await make_client(http, sleeps).chat([{"role": "user", "content": "x"}])

    async def test_auth_error_is_not_retried(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        route = router.post(OLLAMA_URL).respond(401, json={"error": "unauthorized"})
        with pytest.raises(OllamaAuthError):
            await make_client(http, sleeps).generate_copy(REQUEST)
        assert route.call_count == 1

    async def test_model_not_found(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        router.post(OLLAMA_URL).respond(404)
        with pytest.raises(OllamaError, match="test-model"):
            await make_client(http, sleeps).generate_copy(REQUEST)

    async def test_other_client_error(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        router.post(OLLAMA_URL).respond(400, json={"error": "bad"})
        with pytest.raises(OllamaError, match="HTTP 400"):
            await make_client(http, sleeps).generate_copy(REQUEST)

    async def test_invalid_json_body(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        router.post(OLLAMA_URL).respond(200, text="<html>proxy</html>")
        with pytest.raises(OllamaResponseError, match="invalid JSON"):
            await make_client(http, sleeps).generate_copy(REQUEST)

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ([], "Unexpected response"),
            ({"error": "model overloaded"}, "model overloaded"),
            (
                {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
                "max_tokens|OLLAMA_MAX_TOKENS",
            ),
            ({"choices": [{"message": {"content": "  "}}]}, "empty answer"),
            ({"message": {"content": ""}, "done_reason": "length"}, "OLLAMA_MAX_TOKENS"),
            ({"unexpected": True}, "empty answer"),
        ],
    )
    async def test_envelope_errors(
        self,
        router: respx.MockRouter,
        http: httpx.AsyncClient,
        sleeps: Sleeps,
        payload: object,
        message: str,
    ) -> None:
        router.post(OLLAMA_URL).respond(200, json=payload)
        with pytest.raises(OllamaError, match=message):
            await make_client(http, sleeps).generate_copy(REQUEST)

    async def test_repair_round_recovers(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        route = router.post(OLLAMA_URL).mock(
            side_effect=[
                httpx.Response(200, json=ollama_reply("<think>hmm</think>#only #tags")),
                httpx.Response(200, json=ollama_reply(GOOD_COPY)),
            ]
        )
        copy = await make_client(http, sleeps).generate_copy(REQUEST)
        assert copy.repaired
        assert copy.caption.startswith("Fetch mode")
        repair_messages = json.loads(route.calls.last.request.content)["messages"]
        assert [m["role"] for m in repair_messages] == ["system", "user", "assistant", "user"]

    async def test_repair_round_fails(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        router.post(OLLAMA_URL).respond(200, json=ollama_reply("#only #tags"))
        with pytest.raises(OllamaResponseError):
            await make_client(http, sleeps).generate_copy(REQUEST)

    async def test_not_configured(
        self, router: respx.MockRouter, http: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        client = make_client(
            http, sleeps, endpoint="https://ollama.com/v1/chat/completions", api_key=None
        )
        assert not client.configured
        with pytest.raises(OllamaError, match="not configured"):
            await client.generate_copy(REQUEST)
