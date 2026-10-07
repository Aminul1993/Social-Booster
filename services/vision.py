"""Image tagging with a torchvision ResNet-50 (ImageNet) on the CPU.

Design
------
* The model is created once per process (lazy, guarded by a lock) and shared.
* Inference is synchronous PyTorch code; it runs on a dedicated
  :class:`~concurrent.futures.ThreadPoolExecutor` so the event loop never
  blocks. An :class:`asyncio.Semaphore` bounds concurrent inferences, which
  together with ``torch.set_num_threads`` prevents CPU oversubscription when
  several uploads arrive at once.
* Class names come from the weights' metadata, so no ``imagenet_classes.txt``
  download is needed.
* ``torch`` is imported lazily: the module imports fine without it, and the
  ``disabled`` backend lets the app run (with manual keywords) on hosts that
  cannot afford the ~1 GB PyTorch footprint.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from PIL import Image

from services.errors import VisionError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

VisionBackend = Literal["resnet50", "disabled"]


@dataclass(frozen=True, slots=True)
class VisionLabel:
    """A visual concept detected in an image with the model's confidence."""

    name: str
    score: float


@dataclass(frozen=True, slots=True)
class VisionConfig:
    """Runtime configuration for :class:`VisionService`."""

    backend: VisionBackend = "resnet50"
    weights: str | None = "IMAGENET1K_V2"
    top_k: int = 5
    min_confidence: float = 0.0
    num_threads: int = 2
    max_concurrency: int = 2
    warmup: bool = True
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if self.num_threads < 1 or self.max_concurrency < 1:
            raise ValueError("num_threads and max_concurrency must be >= 1")


class ImageClassifier(Protocol):
    """Anything that can turn a PIL image into ranked labels."""

    def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
        """Return the ``top_k`` most likely labels, best first."""
        ...


class ResNet50Classifier:
    """torchvision ResNet-50 classifier tuned for CPU inference.

    Args:
        weights: Name of a ``ResNet50_Weights`` member (``IMAGENET1K_V2``,
            ``IMAGENET1K_V1``, ``DEFAULT``) or ``None`` for random
            initialisation (tests only; predictions are meaningless).
        num_threads: Intra-op threads PyTorch may use for this process.
    """

    def __init__(self, *, weights: str | None = "IMAGENET1K_V2", num_threads: int = 2) -> None:
        import torch
        from torchvision.models import ResNet50_Weights, resnet50

        torch.set_num_threads(num_threads)
        # Inter-op threads can only be set once per process (before any parallel work).
        with contextlib.suppress(RuntimeError):
            torch.set_num_interop_threads(1)

        weights_enum = ResNet50_Weights[weights] if weights else None
        # Category names and preprocessing are static metadata: available even
        # when the weights themselves are not downloaded (weights=None).
        reference = weights_enum or ResNet50_Weights.IMAGENET1K_V2
        self._categories: list[str] = list(reference.meta["categories"])
        self._preprocess: Callable[[Image.Image], Any] = reference.transforms()

        model = resnet50(weights=weights_enum)
        model.eval()
        self._model = model.to(memory_format=torch.channels_last)
        self._torch = torch

    @property
    def categories(self) -> Sequence[str]:
        return self._categories

    def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
        torch = self._torch
        tensor = self._preprocess(image.convert("RGB")).unsqueeze(0)
        tensor = tensor.contiguous(memory_format=torch.channels_last)
        with torch.inference_mode():
            probabilities = self._model(tensor).softmax(dim=1)[0]
            scores, indices = probabilities.topk(min(top_k, len(self._categories)))
        return [
            VisionLabel(name=self._categories[index], score=float(score))
            for score, index in zip(scores.tolist(), indices.tolist(), strict=True)
        ]


ClassifierFactory = Callable[[], ImageClassifier]


class VisionService:
    """Async façade over an :class:`ImageClassifier`."""

    def __init__(
        self,
        config: VisionConfig,
        *,
        classifier_factory: ClassifierFactory | None = None,
    ) -> None:
        self.config = config
        self._factory: ClassifierFactory = classifier_factory or self._default_factory
        self._classifier: ImageClassifier | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrency, thread_name_prefix="vision"
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._load_lock = asyncio.Lock()
        self._load_error: str | None = None

    # ------------------------------------------------------------------ status
    @property
    def enabled(self) -> bool:
        return self.config.backend != "disabled"

    @property
    def ready(self) -> bool:
        return self._classifier is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def health(self) -> dict[str, object]:
        """Component status for the readiness endpoint."""
        if not self.enabled:
            return {"status": "disabled"}
        if self._classifier is not None:
            return {"status": "ok", "backend": self.config.backend}
        if self._load_error:
            return {"status": "error", "error": self._load_error}
        return {"status": "loading"}

    # --------------------------------------------------------------- lifecycle
    def _default_factory(self) -> ImageClassifier:
        return ResNet50Classifier(weights=self.config.weights, num_threads=self.config.num_threads)

    async def start(self) -> None:
        """Load (and warm up) the model. Failures are recorded, not raised.

        The application stays usable without vision: users can type keywords.
        """
        if not self.enabled:
            logger.info("Vision backend disabled")
            return
        try:
            await self._ensure_loaded()
        except VisionError:
            logger.exception("Vision model failed to load; continuing without image tagging")

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _ensure_loaded(self) -> ImageClassifier:
        if self._classifier is not None:
            return self._classifier
        async with self._load_lock:
            if self._classifier is None:
                started = time.perf_counter()
                try:
                    self._classifier = await self._run(self._build)
                except Exception as exc:
                    self._load_error = f"{type(exc).__name__}: {exc}"
                    raise VisionError("The image-tagging model is unavailable.") from exc
                self._load_error = None
                logger.info(
                    "Vision model ready",
                    extra={
                        "backend": self.config.backend,
                        "load_seconds": round(time.perf_counter() - started, 2),
                    },
                )
        return self._classifier

    def _build(self) -> ImageClassifier:
        classifier = self._factory()
        if self.config.warmup:
            # First inference pays one-off allocation costs; do it before users do.
            classifier.predict(Image.new("RGB", (224, 224)), 1)
        return classifier

    def _run[T](self, func: Callable[..., T], *args: object) -> Awaitable[T]:
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._executor, func, *args)

    # --------------------------------------------------------------- inference
    async def classify(self, image: Image.Image | bytes) -> list[VisionLabel]:
        """Return the top-k labels for an (already validated) image.

        Prefer passing a small decoded image (see :func:`services.images.make_preview`):
        decoding full-size bytes here costs ~4 bytes per pixel for a model that
        only looks at 224x224.

        Raises:
            VisionError: when the model is unavailable, times out or fails.
        """
        if not self.enabled:
            return []
        classifier = await self._ensure_loaded()
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._run(self._infer, classifier, image),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError as exc:
                raise VisionError("Image analysis timed out.", retryable=True) from exc
            except VisionError:
                raise
            except Exception as exc:
                logger.exception("Vision inference failed")
                raise VisionError("Image analysis failed.") from exc

    def _infer(self, classifier: ImageClassifier, image: Image.Image | bytes) -> list[VisionLabel]:
        if isinstance(image, bytes):
            with Image.open(io.BytesIO(image)) as opened:
                opened.load()
                return self._infer(classifier, opened)
        predictions = classifier.predict(image, self.config.top_k)
        labels: list[VisionLabel] = []
        seen: set[str] = set()
        for prediction in predictions:
            name = " ".join(prediction.name.replace("_", " ").split())
            if not name or name.casefold() in seen:
                continue
            if prediction.score < self.config.min_confidence:
                continue
            seen.add(name.casefold())
            labels.append(VisionLabel(name=name, score=round(prediction.score, 4)))
        return labels
