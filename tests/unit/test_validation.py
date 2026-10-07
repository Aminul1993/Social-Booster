from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from starlette.datastructures import Headers, UploadFile

from app.models import ScheduleForm
from app.validation import (
    UploadTooLargeError,
    parse_local_datetime,
    read_upload,
    validate_copy,
    validate_schedule,
)
from services.publishing import PublishMode

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
DRAFT_ID = "f" * 32


def form(**overrides: object) -> ScheduleForm:
    values: dict[str, object] = {
        "draft_id": DRAFT_ID,
        "caption": "Hello",
        "hashtags": "#a b",
        "profile_ids": ["p1"],
        "mode": "schedule",
        "scheduled_for": "2026-06-01T15:30",
        "timezone": "Europe/Berlin",
    }
    values.update(overrides)
    return ScheduleForm(**values)  # type: ignore[arg-type]


class TestReadUpload:
    async def test_reads_within_limit(self) -> None:
        upload = UploadFile(io.BytesIO(b"x" * 10), filename="a.png")
        assert await read_upload(upload, max_bytes=10) == b"x" * 10

    async def test_declared_size_too_large(self) -> None:
        upload = UploadFile(io.BytesIO(b"x"), size=99, filename="a.png")
        with pytest.raises(UploadTooLargeError):
            await read_upload(upload, max_bytes=10)

    async def test_streamed_size_too_large(self) -> None:
        upload = UploadFile(
            io.BytesIO(b"x" * (2 * 1024 * 1024 + 1)), filename="a.png", headers=Headers()
        )
        with pytest.raises(UploadTooLargeError):
            await read_upload(upload, max_bytes=2 * 1024 * 1024)


class TestValidateCopy:
    def test_normalises(self) -> None:
        edit, errors = validate_copy("  Hello  world ", "a, #b #a")
        assert errors == {}
        assert edit.caption == "Hello world"
        assert edit.hashtags == ["#a", "#b"]

    def test_errors(self) -> None:
        tags = " ".join(f"t{i}" for i in range(40))
        edit, errors = validate_copy("x" * 2300, tags)
        assert set(errors) == {"caption", "hashtags"}
        assert len(edit.hashtags) == 30


class TestValidateSchedule:
    def test_valid_schedule_converts_timezone(self) -> None:
        result = validate_schedule(form(), allowed_profile_ids={"p1", "p2"}, now=NOW)
        assert result.ok
        assert result.result is not None
        assert result.result.scheduled_at == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        assert result.result.profile_ids == ("p1",)
        assert result.result.hashtags == ["#a", "#b"]
        assert result.result.mode is PublishMode.SCHEDULE

    def test_offset_in_value_wins(self) -> None:
        result = validate_schedule(
            form(scheduled_for="2026-06-01T15:30+00:00"), allowed_profile_ids={"p1"}, now=NOW
        )
        assert result.result is not None
        assert result.result.scheduled_at == datetime(2026, 6, 1, 15, 30, tzinfo=UTC)

    @pytest.mark.parametrize("mode", ["queue", "now"])
    def test_queue_and_now_ignore_time(self, mode: str) -> None:
        result = validate_schedule(
            form(mode=mode, scheduled_for="", timezone=""), allowed_profile_ids={"p1"}, now=NOW
        )
        assert result.result is not None
        assert result.result.scheduled_at is None
        assert result.result.timezone == "UTC"

    def test_profiles_deduplicated(self) -> None:
        result = validate_schedule(
            form(profile_ids=["p1", "p1", ""]), allowed_profile_ids={"p1"}, now=NOW
        )
        assert result.result is not None
        assert result.result.profile_ids == ("p1",)

    @pytest.mark.parametrize(
        ("overrides", "field", "message"),
        [
            ({"caption": "   "}, "caption", "Write a caption"),
            ({"profile_ids": []}, "profile_ids", "at least one"),
            ({"profile_ids": ["evil"]}, "profile_ids", "no longer available"),
            ({"timezone": "Mars/Olympus"}, "timezone", "Unknown timezone"),
            ({"timezone": "../../etc"}, "timezone", "Unknown timezone"),
            ({"mode": "yesterday"}, "mode", "Choose when"),
            ({"scheduled_for": ""}, "scheduled_for", "Pick a date"),
            ({"scheduled_for": "tomorrow-ish"}, "scheduled_for", "valid date"),
            ({"scheduled_for": "2026-06-01T11:00", "timezone": "UTC"}, "scheduled_for", "future"),
            ({"scheduled_for": "2027-07-01T11:00"}, "scheduled_for", "one year"),
        ],
    )
    def test_errors(self, overrides: dict[str, object], field: str, message: str) -> None:
        result = validate_schedule(form(**overrides), allowed_profile_ids={"p1"}, now=NOW)
        assert not result.ok
        assert message in result.errors[field]

    def test_parse_local_datetime(self) -> None:
        assert parse_local_datetime("2026-01-01T00:00", "America/New_York") == datetime(
            2026, 1, 1, 5, 0, tzinfo=UTC
        )
        with pytest.raises(ValueError, match=r"Invalid isoformat"):
            parse_local_datetime("nope", "UTC")

    def test_default_now(self) -> None:
        future = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        result = validate_schedule(
            form(scheduled_for=future, timezone="UTC"), allowed_profile_ids={"p1"}
        )
        assert result.ok
