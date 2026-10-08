from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.errors import ServiceError, StorageError
from services.publishing import PostRequest, PublishingProfile, PublishMode


@pytest.mark.parametrize(
    ("service", "label"),
    [
        ("instagram", "Instagram"),
        ("twitter", "X / Twitter"),
        ("google_business", "Google Business"),
    ],
)
def test_profile_service_label(service: str, label: str) -> None:
    assert PublishingProfile(id="1", service=service, username="u").service_label == label


def test_publish_mode_labels() -> None:
    assert [mode.label for mode in PublishMode] == ["Schedule", "Add to queue", "Share now"]


class TestPostRequest:
    def test_valid(self) -> None:
        post = PostRequest(profile_ids=("1",), text="hi", mode=PublishMode.NOW)
        assert post.scheduled_at is None

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"profile_ids": ()}, "profile"),
            ({"text": "  "}, "empty"),
            ({"scheduled_at": None}, "required"),
            ({"scheduled_at": datetime(2030, 1, 1)}, "timezone-aware"),
        ],
    )
    def test_invalid(self, kwargs: dict[str, object], message: str) -> None:
        values: dict[str, object] = {
            "profile_ids": ("1",),
            "text": "hi",
            "mode": PublishMode.SCHEDULE,
            "scheduled_at": datetime(2030, 1, 1, tzinfo=UTC),
        }
        values.update(kwargs)
        with pytest.raises(ValueError, match=message):
            PostRequest(**values)  # type: ignore[arg-type]


def test_service_error_attributes() -> None:
    error = StorageError("disk", retryable=True, status_code=507, retry_after=3.0)
    assert isinstance(error, ServiceError)
    assert (error.message, error.retryable, error.status_code, error.retry_after) == (
        "disk",
        True,
        507,
        3.0,
    )
    assert error.service == "storage"
