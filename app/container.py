"""Composition root: builds every service from :class:`~app.config.Settings`.

Routes receive services through FastAPI dependencies that read this container
from ``app.state`` (see :mod:`app.dependencies`). Tests build the app with
their own settings and may inject a fake image classifier or HTTP transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app import __version__
from app.accounts import PublisherAccounts
from app.config import Settings
from app.db import Database
from app.drafts import DraftLimits, DraftService
from app.metrics import AppMetrics
from app.rate_limit import RateLimiter
from app.repositories import DraftRepository, TokenRepository
from app.security import TokenCipher
from services.buffer import BufferClient, BufferConfig
from services.images import ImageProcessor
from services.ollama import OllamaClient, OllamaConfig
from services.retry import RetryPolicy
from services.storage import LocalFileStorage, Storage
from services.vision import ClassifierFactory, VisionConfig, VisionService

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 3600


@dataclass
class ServiceContainer:
    settings: Settings
    metrics: AppMetrics
    http: httpx.AsyncClient
    database: Database
    storage: Storage
    vision: VisionService
    ollama: OllamaClient
    accounts: PublisherAccounts
    tokens: TokenRepository
    drafts: DraftService
    rate_limiter: RateLimiter
    started_at: float = field(default_factory=time.monotonic)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.database.connect()
        self._tasks.append(asyncio.create_task(self._load_vision(), name="vision-load"))
        if self.settings.draft_retention_hours > 0:
            self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
        logger.info(
            "Application started",
            extra={
                "version": __version__,
                "environment": self.settings.environment.value,
                "vision_backend": self.settings.vision_backend,
                "ollama_configured": self.ollama.configured,
                "buffer_configured": self.accounts.publisher.configured,
            },
        )

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await self.vision.close()
        await self.http.aclose()
        await self.database.close()
        logger.info("Application stopped")

    async def _load_vision(self) -> None:
        await self.vision.start()
        self.metrics.vision_ready.set(1 if self.vision.ready else 0)

    async def run_maintenance(self) -> None:
        """Delete expired drafts/images and tokens of expired sessions."""
        retention = timedelta(hours=self.settings.draft_retention_hours)
        await self.drafts.purge_expired(retention)
        session_cutoff = datetime.now(UTC) - timedelta(
            seconds=self.settings.session_max_age_seconds
        )
        await self.tokens.purge_older_than(session_cutoff)

    async def _maintenance(self) -> None:
        while True:
            try:
                await self.run_maintenance()
            except Exception:
                logger.exception("Maintenance run failed")
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

    @property
    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 3)


def build_container(
    settings: Settings,
    *,
    classifier_factory: ClassifierFactory | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> ServiceContainer:
    metrics = AppMetrics()
    http = httpx.AsyncClient(
        transport=http_transport,
        headers={"User-Agent": f"marketing-ai-builder/{__version__}"},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        follow_redirects=False,
    )
    database = Database(settings.database_path)
    storage = LocalFileStorage(settings.upload_dir)
    processor = ImageProcessor(
        max_bytes=settings.max_upload_bytes,
        max_pixels=settings.max_image_pixels,
        quality=settings.image_quality,
        max_dimension=settings.image_max_dimension or None,
    )
    vision = VisionService(
        VisionConfig(
            backend=settings.vision_backend,
            weights=settings.vision_weights or None,
            top_k=settings.vision_top_k,
            min_confidence=settings.vision_min_confidence,
            num_threads=settings.vision_num_threads,
            max_concurrency=settings.vision_max_concurrency,
            warmup=settings.vision_warmup,
            timeout_seconds=settings.vision_timeout_seconds,
        ),
        classifier_factory=classifier_factory,
    )
    ollama = OllamaClient(
        OllamaConfig(
            endpoint=settings.ollama_endpoint,
            model=settings.ollama_model,
            api_key=Settings.secret_value(settings.ollama_api_key),
            temperature=settings.ollama_temperature,
            max_tokens=settings.ollama_max_tokens,
            timeout_seconds=settings.ollama_timeout_seconds,
            retry=RetryPolicy(max_attempts=settings.ollama_max_retries),
        ),
        http,
    )
    buffer = BufferClient(
        BufferConfig(
            client_id=settings.buffer_client_id,
            client_secret=Settings.secret_value(settings.buffer_client_secret),
            redirect_uri=settings.buffer_redirect_uri,
            oauth_url=settings.buffer_oauth_url,
            token_url=settings.buffer_token_url,
            api_url=settings.buffer_api_url,
            scope=settings.buffer_scope,
            timeout_seconds=settings.buffer_timeout_seconds,
            retry=RetryPolicy(max_attempts=settings.buffer_max_retries),
        ),
        http,
    )
    cipher = TokenCipher.from_secrets(
        session_secret=settings.session_secret_value,
        explicit_key=Settings.secret_value(settings.token_encryption_key),
    )
    tokens = TokenRepository(database, cipher)
    accounts = PublisherAccounts(
        buffer, tokens, cache_ttl_seconds=settings.buffer_profiles_cache_seconds
    )
    drafts = DraftService(
        repository=DraftRepository(database),
        storage=storage,
        processor=processor,
        vision=vision,
        ollama=ollama,
        metrics=metrics,
        limits=DraftLimits(
            max_upload_bytes=settings.max_upload_bytes,
            max_files_per_upload=settings.max_files_per_upload,
            max_drafts_per_session=settings.max_drafts_per_session,
            max_concurrent_images=settings.image_max_concurrency,
            caption_max_chars=settings.caption_max_chars,
            hashtags_min=settings.hashtags_min,
            hashtags_max=settings.hashtags_max,
        ),
    )
    return ServiceContainer(
        settings=settings,
        metrics=metrics,
        http=http,
        database=database,
        storage=storage,
        vision=vision,
        ollama=ollama,
        accounts=accounts,
        tokens=tokens,
        drafts=drafts,
        rate_limiter=RateLimiter(),
    )
