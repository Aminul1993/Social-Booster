"""Provider-neutral contracts for social publishing.

Buffer is the only provider today (:mod:`services.buffer`). Adding another
(e.g. Hootsuite or a direct network API) means implementing
:class:`SocialPublisher` and registering it in the application container; the
web layer only depends on these types.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class PublishMode(StrEnum):
    """When a post goes out."""

    SCHEDULE = "schedule"  # at an explicit date/time
    QUEUE = "queue"  # next free slot in the provider's posting schedule
    NOW = "now"  # immediately

    @property
    def label(self) -> str:
        return {
            PublishMode.SCHEDULE: "Schedule",
            PublishMode.QUEUE: "Add to queue",
            PublishMode.NOW: "Share now",
        }[self]


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """OAuth credentials for one connected account."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scope: str | None = None

    def is_expired(self, *, now: datetime | None = None, leeway: int = 60) -> bool:
        if self.expires_at is None:
            return False
        current = now or datetime.now(UTC)
        return self.expires_at <= current + timedelta(seconds=leeway)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthToken:
        expires_raw = data.get("expires_at")
        return cls(
            access_token=str(data["access_token"]),
            token_type=str(data.get("token_type") or "bearer"),
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(expires_raw) if expires_raw else None,
            scope=data.get("scope"),
        )


@dataclass(frozen=True, slots=True)
class PublishingProfile:
    """A social account (Instagram page, X handle, LinkedIn page...) in the provider."""

    id: str
    service: str
    username: str
    avatar_url: str | None = None

    @property
    def service_label(self) -> str:
        names = {
            "twitter": "X / Twitter",
            "x": "X / Twitter",
            "facebook": "Facebook",
            "instagram": "Instagram",
            "linkedin": "LinkedIn",
            "pinterest": "Pinterest",
            "tiktok": "TikTok",
            "mastodon": "Mastodon",
            "threads": "Threads",
            "youtube": "YouTube",
            "googlebusiness": "Google Business",
            "bluesky": "Bluesky",
        }
        return names.get(self.service.lower(), self.service.replace("_", " ").title())


@dataclass(frozen=True, slots=True)
class PostRequest:
    """Everything needed to publish one post to one or more profiles."""

    profile_ids: tuple[str, ...]
    text: str
    mode: PublishMode
    media_url: str | None = None
    scheduled_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.profile_ids:
            raise ValueError("At least one profile is required")
        if not self.text.strip():
            raise ValueError("Post text must not be empty")
        if self.mode is PublishMode.SCHEDULE:
            if self.scheduled_at is None:
                raise ValueError("scheduled_at is required in schedule mode")
            if self.scheduled_at.tzinfo is None:
                raise ValueError("scheduled_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Provider response for a successful publish call."""

    update_ids: tuple[str, ...]
    message: str | None = None


@runtime_checkable
class SocialPublisher(Protocol):
    """Contract every publishing provider implements."""

    provider: str
    display_name: str

    @property
    def configured(self) -> bool:
        """Whether OAuth client credentials are present."""
        ...

    def authorization_url(self, state: str) -> str:
        """URL of the provider's consent screen."""
        ...

    async def exchange_code(self, code: str) -> OAuthToken:
        """Exchange an authorization code for an access token."""
        ...

    async def list_profiles(self, token: OAuthToken) -> list[PublishingProfile]:
        """Profiles the token may post to."""
        ...

    async def publish(self, token: OAuthToken, post: PostRequest) -> PublishResult:
        """Create (or schedule) the post."""
        ...
