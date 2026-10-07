"""Request-level validation that needs more context than a Pydantic field.

Kept separate from the routes so every rule is unit-testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.datastructures import UploadFile

from app.models import ScheduleForm
from services.content import MAX_CAPTION_LENGTH, MAX_HASHTAGS, normalize_caption, normalize_hashtags
from services.publishing import PublishMode

MIN_SCHEDULE_LEAD = timedelta(minutes=1)
MAX_SCHEDULE_AHEAD = timedelta(days=365)
_READ_CHUNK = 1024 * 1024


class UploadTooLargeError(ValueError):
    """The uploaded file exceeded the configured size limit."""


async def read_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    """Read an upload fully, aborting as soon as it exceeds ``max_bytes``."""
    if upload.size is not None and upload.size > max_bytes:
        raise UploadTooLargeError
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_READ_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class CopyEdit:
    caption: str
    hashtags: list[str]


def validate_copy(caption: str, hashtags: str) -> tuple[CopyEdit, dict[str, str]]:
    """Normalise caption + hashtags and report problems per field."""
    errors: dict[str, str] = {}
    raw_caption = caption.strip()
    if len(raw_caption) > MAX_CAPTION_LENGTH:
        errors["caption"] = f"Captions can be at most {MAX_CAPTION_LENGTH:,} characters."
    tags = normalize_hashtags(hashtags, limit=MAX_HASHTAGS + 1)
    if len(tags) > MAX_HASHTAGS:
        errors["hashtags"] = f"Use at most {MAX_HASHTAGS} hashtags."
        tags = tags[:MAX_HASHTAGS]
    return CopyEdit(caption=normalize_caption(raw_caption), hashtags=tags), errors


@dataclass(frozen=True, slots=True)
class ValidatedSchedule:
    caption: str
    hashtags: list[str]
    profile_ids: tuple[str, ...]
    mode: PublishMode
    scheduled_at: datetime | None
    timezone: str


@dataclass(slots=True)
class ScheduleValidation:
    result: ValidatedSchedule | None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result is not None


def parse_local_datetime(value: str, timezone: str) -> datetime:
    """Parse an ``<input type=datetime-local>`` value in the user's IANA timezone."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(UTC)


def validate_schedule(
    form: ScheduleForm,
    *,
    allowed_profile_ids: set[str],
    now: datetime | None = None,
) -> ScheduleValidation:
    """Validate everything needed to publish a draft.

    Returns field errors keyed by form field name so the card can highlight
    them inline.
    """
    current = now or datetime.now(UTC)
    copy, errors = validate_copy(form.caption, form.hashtags)
    if not copy.caption:
        errors["caption"] = "Write a caption before publishing."

    profile_ids = tuple(dict.fromkeys(pid for pid in form.profile_ids if pid))
    if not profile_ids:
        errors["profile_ids"] = "Choose at least one profile."
    elif not set(profile_ids) <= allowed_profile_ids:
        errors["profile_ids"] = "One of the selected profiles is no longer available."

    timezone = form.timezone.strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors["timezone"] = "Unknown timezone."
        timezone = "UTC"

    try:
        mode = PublishMode(form.mode)
    except ValueError:
        errors["mode"] = "Choose when the post should go out."
        mode = PublishMode.SCHEDULE

    scheduled_at: datetime | None = None
    if mode is PublishMode.SCHEDULE:
        if not form.scheduled_for.strip():
            errors["scheduled_for"] = "Pick a date and time."
        elif "timezone" not in errors:
            try:
                scheduled_at = parse_local_datetime(form.scheduled_for, timezone)
            except ValueError:
                errors["scheduled_for"] = "Enter a valid date and time."
            else:
                if scheduled_at < current + MIN_SCHEDULE_LEAD:
                    errors["scheduled_for"] = "Pick a time at least one minute in the future."
                elif scheduled_at > current + MAX_SCHEDULE_AHEAD:
                    errors["scheduled_for"] = "Posts can be scheduled at most one year ahead."

    if errors:
        return ScheduleValidation(result=None, errors=errors)
    return ScheduleValidation(
        result=ValidatedSchedule(
            caption=copy.caption,
            hashtags=copy.hashtags,
            profile_ids=profile_ids,
            mode=mode,
            scheduled_at=scheduled_at,
            timezone=timezone,
        )
    )
