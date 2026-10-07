"""Draft workflow: upload -> tag -> generate copy -> edit -> publish.

This is the application-service layer: it orchestrates storage, the vision
model, the AI client and the repository, and is free of HTTP concerns (routes
only translate forms to calls and results to templates).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import UploadFile
from PIL import Image

from app.errors import BadRequestError, NotFoundError
from app.metrics import AppMetrics
from app.models import Draft, DraftStatus, Label, PublishRecord
from app.repositories import DraftRepository
from app.validation import CopyEdit, UploadTooLargeError, read_upload, validate_copy
from services.content import normalize_keywords
from services.errors import OllamaError, StorageError, VisionError
from services.images import (
    ImageProcessor,
    InvalidImageError,
    ProcessedImage,
    format_megabytes,
    safe_display_name,
)
from services.ollama import OllamaClient
from services.prompts import CopyRequest, Tone
from services.storage import Storage
from services.vision import VisionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DraftLimits:
    max_upload_bytes: int
    max_files_per_upload: int
    max_drafts_per_session: int
    #: Images decoded at once per process; each needs ~4 bytes per pixel.
    max_concurrent_images: int = 1
    caption_max_chars: int = 150
    hashtags_min: int = 5
    hashtags_max: int = 8


@dataclass(slots=True)
class UploadOutcome:
    drafts: list[Draft] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


class KeywordsRequiredError(BadRequestError):
    default_message = "Add at least one keyword describing the image."


class DraftService:
    def __init__(
        self,
        *,
        repository: DraftRepository,
        storage: Storage,
        processor: ImageProcessor,
        vision: VisionService,
        ollama: OllamaClient,
        metrics: AppMetrics,
        limits: DraftLimits,
    ) -> None:
        self._repo = repository
        self._storage = storage
        self._processor = processor
        self._vision = vision
        self._ollama = ollama
        self._metrics = metrics
        self.limits = limits
        # Shared by all requests of this worker, not per upload batch.
        self._decoding = asyncio.Semaphore(limits.max_concurrent_images)

    # ------------------------------------------------------------------ queries
    async def list_for_session(self, session_id: str) -> list[Draft]:
        return await self._repo.list_for_session(
            session_id, limit=self.limits.max_drafts_per_session
        )

    async def get(self, session_id: str, draft_id: str) -> Draft:
        draft = await self._repo.get(draft_id, session_id)
        if draft is None:
            raise NotFoundError("That draft no longer exists. Reload the page.")
        return draft

    def public_path(self, draft: Draft) -> str:
        return self._storage.public_path(draft.image_key)

    # ------------------------------------------------------------------- upload
    async def upload(self, session_id: str, files: list[UploadFile]) -> UploadOutcome:
        """Validate, store and tag every file; bad files are reported, not fatal."""
        files = [f for f in files if f.filename or (f.size or 0) > 0]
        if not files:
            raise BadRequestError("Choose at least one image to upload.")
        if len(files) > self.limits.max_files_per_upload:
            raise BadRequestError(
                f"You can upload at most {self.limits.max_files_per_upload} images at once."
            )
        existing = await self._repo.count_for_session(session_id)
        room = self.limits.max_drafts_per_session - existing
        if room <= 0:
            raise BadRequestError(
                "You have reached the maximum number of drafts. Delete some to upload more."
            )

        outcome = UploadOutcome()
        accepted, overflow = files[:room], files[room:]
        results = await asyncio.gather(
            *(self._ingest(session_id, upload) for upload in accepted), return_exceptions=True
        )
        for upload, result in zip(accepted, results, strict=True):
            name = safe_display_name(upload.filename)
            if isinstance(result, Draft):
                outcome.drafts.append(result)
                self._metrics.uploads.labels(outcome="accepted").inc()
            elif isinstance(result, InvalidImageError | UploadTooLargeError | StorageError):
                reason = (
                    f"larger than {format_megabytes(self.limits.max_upload_bytes)}"
                    if isinstance(result, UploadTooLargeError)
                    else str(result)
                )
                outcome.rejected.append((name, reason))
                self._metrics.uploads.labels(outcome="rejected").inc()
            else:
                raise result
        for upload in overflow:
            outcome.rejected.append((safe_display_name(upload.filename), "draft limit reached"))
        return outcome

    async def _ingest(self, session_id: str, upload: UploadFile) -> Draft:
        processed = await self._process(upload)
        stored = await self._storage.save(
            processed.data,
            extension=processed.format.extension,
            content_type=processed.format.content_type,
        )
        labels, vision_error = await self._tag(processed.preview)
        draft = Draft(
            session_id=session_id,
            image_key=stored.key,
            original_filename=safe_display_name(upload.filename),
            content_type=stored.content_type,
            width=processed.width,
            height=processed.height,
            size_bytes=stored.size,
            labels=labels,
            vision_error=vision_error,
            keywords=[label.name for label in labels],
        )
        try:
            await self._repo.add(draft)
        except Exception:
            await self._storage.delete(stored.key)
            raise
        logger.info(
            "Image uploaded",
            extra={"draft_id": draft.id, "bytes": stored.size, "labels": len(labels)},
        )
        return draft

    async def _process(self, upload: UploadFile) -> ProcessedImage:
        """Read and sanitise one upload while holding a decoding slot.

        Files waiting for a slot stay spooled on disk, so peak memory is bounded
        by ``max_concurrent_images`` rather than by the size of the batch.
        """
        async with self._decoding:
            try:
                raw = await read_upload(upload, max_bytes=self.limits.max_upload_bytes)
            finally:
                await upload.close()
            return await asyncio.to_thread(
                self._processor.process,
                raw,
                declared_content_type=upload.content_type,
                preview=self._vision.enabled,
            )

    async def _tag(self, preview: Image.Image | None) -> tuple[list[Label], str | None]:
        if preview is None or not self._vision.enabled:
            return [], None
        started = time.perf_counter()
        try:
            results = await self._vision.classify(preview)
        except VisionError as exc:
            self._metrics.vision_failures.inc()
            logger.warning("Image tagging failed", extra={"error": exc.message})
            return [], exc.message
        self._metrics.vision_latency.observe(time.perf_counter() - started)
        return [Label(name=r.name, score=min(max(r.score, 0.0), 1.0)) for r in results], None

    # ----------------------------------------------------------------- generate
    async def generate(self, session_id: str, draft_id: str, *, keywords: str, tone: Tone) -> Draft:
        """Ask the AI for a caption + hashtags based on the (edited) keywords."""
        draft = await self.get(session_id, draft_id)
        cleaned = normalize_keywords(keywords)
        if not cleaned:
            raise KeywordsRequiredError()
        request = CopyRequest(
            keywords=tuple(cleaned),
            tone=tone,
            caption_max_chars=self.limits.caption_max_chars,
            min_hashtags=self.limits.hashtags_min,
            max_hashtags=self.limits.hashtags_max,
        )
        started = time.perf_counter()
        try:
            copy = await self._ollama.generate_copy(request)
        except OllamaError:
            self._metrics.ai_generations.labels(outcome="error").inc()
            raise
        finally:
            self._metrics.ai_latency.observe(time.perf_counter() - started)
        self._metrics.ai_generations.labels(outcome="success").inc()
        status = DraftStatus.PUBLISHED if draft.publish else DraftStatus.GENERATED
        updated = draft.model_copy(
            update={
                "keywords": cleaned,
                "tone": tone,
                "caption": copy.caption,
                "hashtags": list(copy.hashtags),
                "ai_model": copy.model,
                "status": status,
            }
        )
        logger.info(
            "Copy generated",
            extra={"draft_id": draft.id, "strategy": copy.strategy, "repaired": copy.repaired},
        )
        return await self._repo.save(updated)

    # --------------------------------------------------------------------- edit
    async def update_copy(
        self, session_id: str, draft_id: str, *, caption: str, hashtags: str
    ) -> tuple[Draft, dict[str, str]]:
        draft = await self.get(session_id, draft_id)
        edit, errors = validate_copy(caption, hashtags)
        if errors:
            return draft, errors
        return await self.apply_copy(draft, edit), {}

    async def apply_copy(self, draft: Draft, edit: CopyEdit) -> Draft:
        if edit.caption == draft.caption and edit.hashtags == draft.hashtags:
            return draft
        return await self._repo.save(
            draft.model_copy(update={"caption": edit.caption, "hashtags": edit.hashtags})
        )

    async def delete(self, session_id: str, draft_id: str) -> None:
        draft = await self._repo.delete(draft_id, session_id)
        if draft is None:
            raise NotFoundError("That draft no longer exists.")
        await self._storage.delete(draft.image_key)

    async def record_publish(self, draft: Draft, record: PublishRecord) -> Draft:
        return await self._repo.save(
            draft.model_copy(update={"publish": record, "status": DraftStatus.PUBLISHED})
        )

    # ---------------------------------------------------------------- retention
    async def purge_expired(self, retention: timedelta) -> int:
        cutoff = datetime.now(UTC) - retention
        keys = await self._repo.purge_older_than(cutoff)
        for key in keys:
            await self._storage.delete(key)
        if keys:
            logger.info("Purged expired drafts", extra={"count": len(keys)})
        return len(keys)
