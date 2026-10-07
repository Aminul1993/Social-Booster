from __future__ import annotations

import asyncio
import importlib.util
import threading

import pytest
from PIL import Image

from services.errors import VisionError
from services.vision import (
    ImageClassifier,
    ResNet50Classifier,
    VisionConfig,
    VisionLabel,
    VisionService,
)
from tests.helpers import FailingClassifier, FakeClassifier, make_image

HAS_TORCH = (
    importlib.util.find_spec("torch") is not None
    and importlib.util.find_spec("torchvision") is not None
)


def service(classifier: type | None = FakeClassifier, **config: object) -> VisionService:
    defaults: dict[str, object] = {"warmup": False, "num_threads": 1}
    defaults.update(config)
    return VisionService(VisionConfig(**defaults), classifier_factory=classifier)  # type: ignore[arg-type]


class TestConfig:
    @pytest.mark.parametrize(
        "kwargs",
        [{"top_k": 0}, {"min_confidence": 1.5}, {"num_threads": 0}, {"max_concurrency": 0}],
    )
    def test_validation(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError, match=r"must be"):
            VisionConfig(**kwargs)  # type: ignore[arg-type]


class TestVisionService:
    async def test_classify_cleans_labels(self) -> None:
        vision = service(top_k=5)
        try:
            labels = await vision.classify(make_image())
        finally:
            await vision.close()
        assert [label.name for label in labels] == [
            "golden retriever",
            "tennis ball",
            "Labrador retriever",
        ]
        assert labels[0].score == pytest.approx(0.8213)
        assert vision.ready

    async def test_min_confidence_and_dedupe(self) -> None:
        class Noisy:
            def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
                return [
                    VisionLabel("cat", 0.6),
                    VisionLabel("Cat", 0.3),
                    VisionLabel("_", 0.2),
                    VisionLabel("dog", 0.01),
                ]

        vision = service(Noisy, min_confidence=0.05)
        try:
            labels = await vision.classify(make_image())
        finally:
            await vision.close()
        assert [label.name for label in labels] == ["cat"]

    async def test_classify_decoded_image(self) -> None:
        vision = service(top_k=1)
        try:
            labels = await vision.classify(Image.new("RGB", (32, 32)))
        finally:
            await vision.close()
        assert [label.name for label in labels] == ["golden retriever"]

    async def test_disabled_backend(self) -> None:
        vision = service(backend="disabled")
        await vision.start()
        assert await vision.classify(make_image()) == []
        assert vision.health() == {"status": "disabled"}
        assert not vision.enabled
        await vision.close()

    async def test_start_loads_once_and_warms_up(self) -> None:
        built: list[FakeClassifier] = []

        def factory() -> ImageClassifier:
            classifier = FakeClassifier()
            built.append(classifier)
            return classifier

        vision = VisionService(VisionConfig(warmup=True), classifier_factory=factory)
        assert vision.health() == {"status": "loading"}
        await asyncio.gather(vision.start(), vision.start(), vision.classify(make_image()))
        assert len(built) == 1
        assert built[0].calls == 2  # warm-up + one real inference
        assert vision.health() == {"status": "ok", "backend": "resnet50"}
        await vision.close()

    async def test_load_failure_is_recorded_not_raised_on_start(self) -> None:
        def broken() -> ImageClassifier:
            raise OSError("weights download failed")

        vision = VisionService(VisionConfig(warmup=False), classifier_factory=broken)
        await vision.start()
        assert not vision.ready
        assert vision.load_error == "OSError: weights download failed"
        assert vision.health()["status"] == "error"
        with pytest.raises(VisionError, match="unavailable"):
            await vision.classify(make_image())
        await vision.close()

    async def test_inference_failure(self) -> None:
        vision = service(FailingClassifier)
        with pytest.raises(VisionError, match="Image analysis failed"):
            await vision.classify(make_image())
        await vision.close()

    async def test_unreadable_bytes(self) -> None:
        vision = service()
        with pytest.raises(VisionError, match="failed"):
            await vision.classify(b"not an image")
        await vision.close()

    async def test_timeout(self) -> None:
        release = threading.Event()

        class Slow:
            def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
                release.wait(5)
                return []

        vision = service(Slow, timeout_seconds=0.05)
        try:
            with pytest.raises(VisionError, match="timed out") as excinfo:
                await vision.classify(make_image())
            assert excinfo.value.retryable
        finally:
            release.set()
            await vision.close()

    async def test_concurrency_is_bounded(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        class Tracking:
            def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                threading.Event().wait(0.02)
                with lock:
                    active -= 1
                return [VisionLabel("x", 0.5)]

        vision = service(Tracking, max_concurrency=2)
        await asyncio.gather(*(vision.classify(make_image()) for _ in range(6)))
        await vision.close()
        assert peak <= 2


@pytest.mark.torch
@pytest.mark.skipif(not HAS_TORCH, reason="torch/torchvision not installed")
class TestResNet50:
    """Runs the real ResNet-50 graph with random weights (no download)."""

    def test_predict_shapes_and_categories(self) -> None:
        classifier = ResNet50Classifier(weights=None, num_threads=1)
        assert len(classifier.categories) == 1000
        labels = classifier.predict(Image.new("RGB", (300, 200), "orange"), top_k=5)
        assert len(labels) == 5
        assert all(label.name in classifier.categories for label in labels)
        assert all(0.0 <= label.score <= 1.0 for label in labels)
        assert [label.score for label in labels] == sorted(
            (label.score for label in labels), reverse=True
        )

    async def test_default_factory_through_service(self) -> None:
        vision = VisionService(VisionConfig(weights=None, warmup=True, num_threads=1, top_k=3))
        try:
            await vision.start()
            labels = await vision.classify(make_image("JPEG", size=(120, 90)))
        finally:
            await vision.close()
        assert vision.ready
        assert 1 <= len(labels) <= 3

    def test_unknown_weights_name(self) -> None:
        with pytest.raises(KeyError):
            ResNet50Classifier(weights="NOT_A_WEIGHT", num_threads=1)
