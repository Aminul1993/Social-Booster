"""Image description with an online vision-language model.

Each upload's small preview is sent to a vision model (Ollama Cloud's free
``gemma4:31b`` by default) that answers with a one-sentence description and a
few keywords. Nothing heavy runs in this process, so a worker stays small.
``VISION_BACKEND=disabled`` skips the call; users then type keywords.

An :class:`asyncio.Semaphore` bounds concurrent requests per worker (provider
rate limits) and ``timeout_seconds`` caps the time spent on one image.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from PIL import Image

from services.content import normalize_keywords
from services.errors import OllamaError, VisionError
from services.ollama import OllamaClient, clean_model_text
from services.prompts import DESCRIPTION_MAX_CHARS, build_describe_messages

logger = logging.getLogger(__name__)

VisionBackend = Literal["ollama", "disabled"]

#: Vision-capable model on Ollama Cloud's free plan (others need a paid plan: HTTP 402).
DEFAULT_VISION_MODEL = "gemma4:31b"


@dataclass(frozen=True, slots=True)
class ImageAnalysis:
    """What the vision model saw in one image."""

    keywords: list[str] = field(default_factory=list)
    description: str | None = None


@dataclass(frozen=True, slots=True)
class VisionConfig:
    """Runtime configuration for :class:`VisionService`."""

    backend: VisionBackend = "ollama"
    top_k: int = 5
    max_concurrency: int = 2
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")


class ImageDescriber(Protocol):
    """A (remote) model that describes a PIL image."""

    @property
    def configured(self) -> bool:
        """Whether the credentials it needs are present."""
        ...

    @property
    def model(self) -> str:
        """Model name, for logs and the readiness endpoint."""
        ...

    async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
        """Describe ``image`` with a sentence and up to ``max_keywords`` keywords."""
        ...


def encode_jpeg_base64(image: Image.Image, *, quality: int = 85) -> str:
    """Base64 JPEG of ``image`` (send the small preview, not the original)."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def parse_analysis(raw: str, *, max_keywords: int) -> ImageAnalysis:
    """Parse the ``{"description", "keywords"}`` JSON a vision model returned.

    Raises:
        VisionError: when the reply holds neither a description nor keywords.
    """
    text = clean_model_text(raw)
    start, end = text.find("{"), text.rfind("}")
    data: Any = None
    if start != -1 and end > start:
        with contextlib.suppress(ValueError):
            data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise VisionError("The image description could not be read.")
    raw_keywords = data.get("keywords")
    if isinstance(raw_keywords, str):
        raw_keywords = [raw_keywords]
    keywords = normalize_keywords(
        [str(k) for k in raw_keywords] if isinstance(raw_keywords, list) else [],
        limit=max_keywords,
    )
    raw_description = data.get("description")
    description: str | None = None
    if isinstance(raw_description, str):
        description = " ".join(raw_description.split())[:DESCRIPTION_MAX_CHARS] or None
    if not keywords and not description:
        raise VisionError("The image description was empty.")
    return ImageAnalysis(keywords=keywords, description=description)


class OllamaImageDescriber:
    """:class:`ImageDescriber` backed by a vision model behind :class:`OllamaClient`.

    Works with Ollama Cloud, self-hosted Ollama and other OpenAI-compatible
    chat-completions endpoints that accept ``image_url`` content parts.
    """

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    @property
    def configured(self) -> bool:
        return self._client.configured

    @property
    def model(self) -> str:
        return self._client.config.model

    async def describe(self, image: Image.Image, max_keywords: int) -> ImageAnalysis:
        encoded = await asyncio.to_thread(encode_jpeg_base64, image)
        raw = await self._client.chat(build_describe_messages(encoded, max_keywords=max_keywords))
        return parse_analysis(raw, max_keywords=max_keywords)


class VisionService:
    """Async façade over the configured :class:`ImageDescriber`."""

    def __init__(self, config: VisionConfig, *, describer: ImageDescriber | None = None) -> None:
        self.config = config
        self._describer = describer
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    @property
    def enabled(self) -> bool:
        return self.config.backend != "disabled"

    @property
    def ready(self) -> bool:
        return self.enabled and self._describer is not None and self._describer.configured

    def health(self) -> dict[str, object]:
        """Component status for the readiness endpoint."""
        if not self.enabled:
            return {"status": "disabled"}
        if self.ready and self._describer is not None:
            return {"status": "ok", "backend": self.config.backend, "model": self._describer.model}
        return {"status": "error", "error": "Image description is not configured."}

    def log_status(self) -> None:
        if not self.enabled:
            logger.info("Image description disabled")
        elif self.ready and self._describer is not None:
            logger.info(
                "Image description via online model", extra={"model": self._describer.model}
            )
        else:
            logger.warning("Image description is not configured (OLLAMA_API_KEY)")

    async def analyze(self, image: Image.Image) -> ImageAnalysis:
        """Describe an (already validated, preferably downscaled) image.

        Raises:
            VisionError: when the model is unavailable, times out or fails.
        """
        if not self.enabled:
            return ImageAnalysis()
        describer = self._describer
        if describer is None or not describer.configured:
            raise VisionError("Image description is not configured (set OLLAMA_API_KEY).")
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    describer.describe(image, self.config.top_k),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError as exc:
                raise VisionError("Image analysis timed out.", retryable=True) from exc
            except VisionError:
                raise
            except OllamaError as exc:
                raise VisionError(exc.message, retryable=exc.retryable) from exc
            except Exception as exc:
                logger.exception("Image description failed")
                raise VisionError("Image analysis failed.") from exc
