"""Typed application settings (Pydantic Settings).

Values are read, in order of precedence, from: explicit keyword arguments,
environment variables, the ``.env`` file and Docker/Kubernetes secret files in
``/run/secrets`` (file name = lower-case variable name). See
``docs/ENVIRONMENT.md`` for the full reference.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from services.buffer import DEFAULT_API_URL, is_legacy_url
from services.ollama import DEFAULT_ENDPOINT, DEFAULT_MODEL
from services.vision import DEFAULT_VISION_MODEL

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
_SECRETS_DIR = Path("/run/secrets")
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$", re.IGNORECASE)
_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """``requests`` allowed per ``period_seconds`` for one client."""

    requests: int
    period_seconds: int

    @classmethod
    def parse(cls, value: str) -> RateLimitRule:
        match = _RATE_RE.match(value)
        if not match:
            raise ValueError(
                f"Invalid rate limit {value!r}; use '<count>/<second|minute|hour|day>'"
            )
        requests = int(match.group(1))
        if requests < 1:
            raise ValueError("Rate limit count must be >= 1")
        return cls(requests=requests, period_seconds=_PERIODS[match.group(2).lower()])


RateLimitScope = Literal["upload", "generate", "schedule", "default"]


def _resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else (BASE_DIR / path)


class Settings(BaseSettings):
    """All runtime configuration. Secrets use :class:`SecretStr` so they never
    appear in ``repr()``, logs or tracebacks."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        secrets_dir=_SECRETS_DIR if _SECRETS_DIR.is_dir() else None,
        # ``VAR=`` (empty) means "use the default", as in .env.example.
        env_ignore_empty=True,
        validate_default=True,
    )

    # ------------------------------------------------------------- application
    app_name: str = "Social Booster"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str | None = None
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    enable_api_docs: bool | None = None
    max_request_body_mb: float | None = Field(default=None, gt=0)

    # ---------------------------------------------------------------- security
    session_secret: SecretStr | None = None
    session_cookie_name: str = "mab_session"
    session_max_age_seconds: int = Field(default=14 * 24 * 3600, ge=300)
    cookie_secure: bool | None = None

    # ------------------------------------------------------------- persistence
    database_path: Path = Path("data/app.db")
    upload_dir: Path = Path("uploads")
    draft_retention_hours: int = Field(default=72, ge=0)
    max_drafts_per_session: int = Field(default=100, ge=1)

    # ------------------------------------------------------------------ uploads
    max_upload_size_mb: float = Field(default=10, gt=0, le=100)
    max_files_per_upload: int = Field(default=10, ge=1, le=50)
    max_image_pixels: int = Field(default=40_000_000, ge=1_000_000)
    image_quality: int = Field(default=90, ge=50, le=100)
    image_max_dimension: int = Field(default=2048, ge=0, le=16384)  # 0 = keep original size
    image_max_concurrency: int = Field(default=1, ge=1, le=16)

    # ------------------------------------------------------------------- vision
    vision_backend: Literal["ollama", "disabled"] = "ollama"
    # Any Ollama or OpenAI-compatible chat endpoint serving a vision-capable model.
    vision_model: str = DEFAULT_VISION_MODEL
    vision_endpoint: str | None = None  # default: OLLAMA_ENDPOINT
    vision_api_key: SecretStr | None = None  # default: OLLAMA_API_KEY
    vision_top_k: int = Field(default=5, ge=1, le=20)
    vision_max_concurrency: int = Field(default=2, ge=1, le=16)
    vision_timeout_seconds: float = Field(default=30.0, gt=0)

    # ------------------------------------------------------------- Ollama Cloud
    ollama_api_key: SecretStr | None = None
    ollama_endpoint: str = DEFAULT_ENDPOINT
    ollama_model: str = DEFAULT_MODEL
    ollama_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    ollama_max_tokens: int = Field(default=512, ge=32, le=8192)
    ollama_timeout_seconds: float = Field(default=30.0, gt=0)
    ollama_max_retries: int = Field(default=3, ge=1, le=10)
    caption_max_chars: int = Field(default=150, ge=40, le=2200)
    hashtags_min: int = Field(default=5, ge=1, le=30)
    hashtags_max: int = Field(default=8, ge=1, le=30)

    # ------------------------------------------------------------------- Buffer
    # Personal API key (Buffer -> Settings -> API); every visitor posts with it.
    buffer_access_token: SecretStr | None = None
    buffer_api_url: str = DEFAULT_API_URL
    buffer_timeout_seconds: float = Field(default=20.0, gt=0)
    buffer_max_retries: int = Field(default=3, ge=1, le=10)
    buffer_profiles_cache_seconds: int = Field(default=300, ge=0)

    # ------------------------------------------------------------ rate limiting
    rate_limit_enabled: bool = True
    rate_limit_upload: str = "20/minute"
    rate_limit_generate: str = "30/minute"
    rate_limit_schedule: str = "20/minute"
    rate_limit_default: str = "120/minute"

    # ------------------------------------------------------------ observability
    metrics_enabled: bool = True
    metrics_token: SecretStr | None = None

    # =============================================================== validators
    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value

    @field_validator(
        "public_base_url",
        "ollama_api_key",
        "buffer_access_token",
        "metrics_token",
        "session_secret",
        "vision_endpoint",
        "vision_api_key",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.match(r"^https?://[^/\s]+", value):
            raise ValueError("PUBLIC_BASE_URL must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator(
        "rate_limit_upload",
        "rate_limit_generate",
        "rate_limit_schedule",
        "rate_limit_default",
    )
    @classmethod
    def _validate_rate(cls, value: str) -> str:
        RateLimitRule.parse(value)
        return value.strip()

    @field_validator("database_path", "upload_dir")
    @classmethod
    def _absolute_paths(cls, value: Path) -> Path:
        return _resolve_path(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.hashtags_min > self.hashtags_max:
            raise ValueError("HASHTAGS_MIN must be <= HASHTAGS_MAX")
        if self.is_production:
            secret = self.session_secret.get_secret_value() if self.session_secret else ""
            if len(secret) < 32:
                raise ValueError(
                    "SESSION_SECRET must be set to a random string of at least 32 characters "
                    "in production"
                )
            if self.cookie_secure is False:
                logger.warning("COOKIE_SECURE=false in production: cookies travel over plain HTTP")
        elif self.session_secret is None:
            # Development convenience: an ephemeral secret (sessions reset on restart).
            self.session_secret = SecretStr(secrets.token_urlsafe(48))
        if is_legacy_url(self.buffer_api_url):
            logger.warning(
                "BUFFER_API_URL points at Buffer's retired v1 API (bufferapp.com), which does "
                "not accept API keys from Buffer's Settings -> API; unset it to use the default"
            )
        return self

    # =============================================================== properties
    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def secure_cookies(self) -> bool:
        return self.is_production if self.cookie_secure is None else self.cookie_secure

    @property
    def docs_enabled(self) -> bool:
        return (not self.is_production) if self.enable_api_docs is None else self.enable_api_docs

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_size_mb * 1024 * 1024)

    @property
    def max_request_body_bytes(self) -> int:
        """Hard cap on any request body; defaults to a full upload batch + 1 MB."""
        if self.max_request_body_mb is not None:
            return int(self.max_request_body_mb * 1024 * 1024)
        return self.max_upload_bytes * self.max_files_per_upload + 1024 * 1024

    @property
    def session_secret_value(self) -> str:
        if self.session_secret is None:  # pragma: no cover - set by _check_consistency
            raise RuntimeError("SESSION_SECRET is not configured")
        return self.session_secret.get_secret_value()

    def rate_limit_rule(self, scope: RateLimitScope) -> RateLimitRule:
        return RateLimitRule.parse(getattr(self, f"rate_limit_{scope}"))

    @staticmethod
    def secret_value(secret: SecretStr | None) -> str | None:
        return secret.get_secret_value() if secret is not None else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached)."""
    return Settings()
