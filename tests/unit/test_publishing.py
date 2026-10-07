from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.errors import ServiceError, StorageError
from services.publishing import OAuthToken, PostRequest, PublishingProfile, PublishMode


class TestOAuthToken:
    def test_roundtrip(self) -> None:
        token = OAuthToken(
            access_token="a",
            refresh_token="r",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            scope="write",
        )
        assert OAuthToken.from_dict(token.to_dict()) == token

    def test_roundtrip_minimal(self) -> None:
        token = OAuthToken(access_token="a")
        assert OAuthToken.from_dict({"access_token": "a", "token_type": None}) == token

    def test_expiry_with_leeway(self) -> None:
        now = datetime(2030, 1, 1, tzinfo=UTC)
        soon = OAuthToken(access_token="a", expires_at=now + timedelta(seconds=30))
        later = OAuthToken(access_token="a", expires_at=now + timedelta(hours=1))
        assert soon.is_expired(now=now)
        assert not later.is_expired(now=now)
        assert not OAuthToken(access_token="a").is_expired(now=now)


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
