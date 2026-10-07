from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from PIL import Image

from services.errors import OllamaError, VisionError
from services.ollama import OllamaClient, OllamaConfig
from services.retry import RetryPolicy
from services.vision import (
    ImageAnalysis,
    ImageDescriber,
    OllamaImageDescriber,
    VisionConfig,
    VisionService,
    encode_jpeg_base64,
    parse_analysis,
)
from tests.helpers import OLLAMA_URL, FakeDescriber, ollama_reply

IMAGE = Image.new("RGB", (8, 8))


def service(describer: ImageDescriber | None, **config: object) -> VisionService:
    return VisionService(VisionConfig(**config), describer=describer)  # type: ignore[arg-type]


class TestConfig:
    @pytest.mark.parametrize("kwargs", [{"top_k": 0}, {"top_k": 51}, {"max_concurrency": 0}])
    def test_validation(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError, match=r"must be"):
            VisionConfig(**kwargs)  # type: ignore[arg-type]


class TestParseAnalysis:
    def test_fenced_json_with_reasoning(self) -> None:
        raw = (
            "<think>a dog?</think>```json\n"
            '{"description": "  A golden   retriever on grass. ",'
            ' "keywords": ["Golden Retriever", "dog", "golden_retriever", "#grass", "", 7]}\n```'
        )
        analysis = parse_analysis(raw, max_keywords=3)
        assert analysis == ImageAnalysis(
            keywords=["Golden Retriever", "dog", "grass"],
            description="A golden retriever on grass.",
        )

    def test_keywords_as_a_string_and_long_description(self) -> None:
        analysis = parse_analysis(
            json.dumps({"description": "x" * 500, "keywords": "beach"}), max_keywords=5
        )
        assert analysis.keywords == ["beach"]
        assert analysis.description is not None
        assert len(analysis.description) == 300

    def test_description_only(self) -> None:
        analysis = parse_analysis('{"description": "A red door."}', max_keywords=5)
        assert analysis == ImageAnalysis(keywords=[], description="A red door.")

    @pytest.mark.parametrize("raw", ["a dog, maybe", "[1, 2]", "{not json}"])
    def test_unreadable(self, raw: str) -> None:
        with pytest.raises(VisionError, match="could not be read"):
            parse_analysis(raw, max_keywords=5)

    def test_empty(self) -> None:
        with pytest.raises(VisionError, match="empty"):
            parse_analysis('{"description": " ", "keywords": []}', max_keywords=5)


class TestOllamaImageDescriber:
    @pytest.fixture
    async def http(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient() as client:
            yield client

    async def test_sends_the_image_and_parses_the_reply(self, http: httpx.AsyncClient) -> None:
        reply = json.dumps({"description": "A dog.", "keywords": ["dog", "lawn"]})
        client = OllamaClient(
            OllamaConfig(
                endpoint=OLLAMA_URL,
                model="gemma4:31b",
                api_key="k",
                retry=RetryPolicy(max_attempts=1),
            ),
            http,
        )
        describer = OllamaImageDescriber(client)
        assert describer.configured
        assert describer.model == "gemma4:31b"
        with respx.mock(assert_all_mocked=True) as router:
            route = router.post(OLLAMA_URL).respond(json=ollama_reply(reply))
            analysis = await describer.describe(Image.new("RGBA", (40, 30), "red"), 4)
        assert analysis == ImageAnalysis(keywords=["dog", "lawn"], description="A dog.")
        body = json.loads(route.calls.last.request.content)
        assert body["model"] == "gemma4:31b"
        assert "up to 4 short lowercase keywords" in body["messages"][1]["content"][0]["text"]
        url = body["messages"][1]["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as sent:
            assert (sent.format, sent.size) == ("JPEG", (40, 30))

    def test_encode_jpeg_base64_handles_alpha(self) -> None:
        encoded = encode_jpeg_base64(Image.new("LA", (8, 8)))
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            assert image.mode == "RGB"


class TestVisionService:
    async def test_analyze(self) -> None:
        describer = FakeDescriber()
        analysis = await service(describer, top_k=3).analyze(Image.new("RGB", (64, 48)))
        assert analysis == describer.analysis
        assert describer.calls == [((64, 48), 3)]

    def test_health_and_status(self, caplog: pytest.LogCaptureFixture) -> None:
        vision = service(FakeDescriber())
        assert vision.ready
        assert vision.health() == {"status": "ok", "backend": "ollama", "model": "fake-vision"}
        with caplog.at_level(logging.INFO, logger="services.vision"):
            vision.log_status()
        assert "via online model" in caplog.text

    @pytest.mark.parametrize("configured", [False, None])
    async def test_not_configured(
        self, configured: bool | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        describer = None
        if configured is not None:
            describer = FakeDescriber()
            describer.configured = configured
        vision = service(describer)
        assert not vision.ready
        assert vision.health() == {
            "status": "error",
            "error": "Image description is not configured.",
        }
        with caplog.at_level(logging.WARNING, logger="services.vision"):
            vision.log_status()
        assert "not configured" in caplog.text
        with pytest.raises(VisionError, match="OLLAMA_API_KEY"):
            await vision.analyze(IMAGE)

    async def test_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        describer = FakeDescriber()
        vision = service(describer, backend="disabled")
        assert not vision.enabled
        assert not vision.ready
        assert vision.health() == {"status": "disabled"}
        with caplog.at_level(logging.INFO, logger="services.vision"):
            vision.log_status()
        assert "disabled" in caplog.text
        assert await vision.analyze(IMAGE) == ImageAnalysis()
        assert describer.calls == []

    async def test_provider_errors_become_vision_errors(self) -> None:
        class Busy(FakeDescriber):
            async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
                raise OllamaError("The AI service is busy (HTTP 503).", retryable=True)

        with pytest.raises(VisionError, match="busy") as excinfo:
            await service(Busy()).analyze(IMAGE)
        assert excinfo.value.retryable

    async def test_vision_errors_pass_through(self) -> None:
        class Unreadable(FakeDescriber):
            async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
                raise VisionError("The image description could not be read.")

        with pytest.raises(VisionError, match="could not be read"):
            await service(Unreadable()).analyze(IMAGE)

    async def test_unexpected_errors(self) -> None:
        class Broken(FakeDescriber):
            async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
                raise RuntimeError("boom")

        with pytest.raises(VisionError, match="Image analysis failed"):
            await service(Broken()).analyze(IMAGE)

    async def test_timeout(self) -> None:
        class Slow(FakeDescriber):
            async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
                await asyncio.sleep(5)
                raise AssertionError("unreachable")

        with pytest.raises(VisionError, match="timed out") as excinfo:
            await service(Slow(), timeout_seconds=0.05).analyze(IMAGE)
        assert excinfo.value.retryable

    async def test_concurrency_is_bounded(self) -> None:
        active = 0
        peak = 0

        class Tracking(FakeDescriber):
            async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
                return ImageAnalysis()

        vision = service(Tracking(), max_concurrency=2)
        await asyncio.gather(*(vision.analyze(IMAGE) for _ in range(6)))
        assert peak == 2
