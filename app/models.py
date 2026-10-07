"""Pydantic schemas: persisted drafts, form payloads and API responses."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from services.content import compose_post_text
from services.prompts import Tone
from services.publishing import PublishMode


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class DraftStatus(StrEnum):
    UPLOADED = "uploaded"
    GENERATED = "generated"
    PUBLISHED = "published"

    @property
    def label(self) -> str:
        return {"uploaded": "Ready", "generated": "Copy ready", "published": "Sent to Buffer"}[
            self.value
        ]

    @property
    def badge(self) -> str:
        return {"uploaded": "secondary", "generated": "primary", "published": "success"}[self.value]


class Label(BaseModel):
    """A visual concept detected by the vision model."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=128)


class PublishRecord(BaseModel):
    """What happened the last time the draft was sent to the provider."""

    provider: str
    mode: PublishMode
    profile_ids: list[str]
    profile_names: list[str] = Field(default_factory=list)
    scheduled_at: datetime | None = None
    update_ids: list[str] = Field(default_factory=list)
    published_at: datetime = Field(default_factory=utcnow)
    message: str | None = None


class Draft(BaseModel):
    """One uploaded image and the copy written for it (persisted per session)."""

    id: str = Field(default_factory=new_id)
    session_id: str
    image_key: str
    original_filename: str
    content_type: str
    width: int
    height: int
    size_bytes: int
    labels: list[Label] = Field(default_factory=list)
    description: str | None = None
    vision_error: str | None = None
    keywords: list[str] = Field(default_factory=list)
    tone: Tone = Tone.FRIENDLY
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    ai_model: str | None = None
    status: DraftStatus = DraftStatus.UPLOADED
    publish: PublishRecord | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def has_copy(self) -> bool:
        return bool(self.caption)

    @property
    def hashtags_text(self) -> str:
        return " ".join(self.hashtags)

    @property
    def keywords_text(self) -> str:
        return ", ".join(self.keywords)

    @property
    def post_text(self) -> str:
        return compose_post_text(self.caption, self.hashtags)


# ----------------------------------------------------------------------- form payloads
class GenerateForm(BaseModel):
    """``POST /generate`` - write copy for a draft."""

    draft_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    keywords: str = Field(default="", max_length=1000)
    tone: Tone = Tone.FRIENDLY


class DraftEditForm(BaseModel):
    """``PATCH /drafts/{id}`` - autosave of the caption and hashtags fields."""

    caption: str = Field(default="", max_length=10_000)
    hashtags: str = Field(default="", max_length=5_000)


class ScheduleForm(BaseModel):
    """``POST /buffer/schedule`` - raw form values (validated in ``app.validation``)."""

    draft_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    caption: str = Field(default="", max_length=10_000)
    hashtags: str = Field(default="", max_length=5_000)
    profile_ids: list[str] = Field(default_factory=list, max_length=50)
    mode: str = Field(default=PublishMode.SCHEDULE.value, max_length=20)
    scheduled_for: str = Field(default="", max_length=40)
    timezone: str = Field(default="UTC", max_length=64)


# --------------------------------------------------------------------------- responses
class ComponentHealth(BaseModel):
    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, ComponentHealth]
