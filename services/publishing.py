"""Provider-neutral contracts for social publishing.

Buffer is the only provider today (:mod:`services.buffer`). Adding another
(e.g. Hootsuite or a direct network API) means implementing
:class:`SocialPublisher` and registering it in the application container; the
web layer only depends on these types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


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
    """Provider response for a (at least partly) successful publish call."""

    update_ids: tuple[str, ...]
    message: str | None = None
    #: ``(profile_id, reason)`` for profiles the provider refused while others succeeded.
    failures: tuple[tuple[str, str], ...] = ()


@runtime_checkable
class SocialPublisher(Protocol):
    """Contract every publishing provider implements."""

    provider: str
    display_name: str

    @property
    def configured(self) -> bool:
        """Whether the provider credentials are present."""
        ...

    async def list_profiles(self) -> list[PublishingProfile]:
        """Profiles the configured account may post to."""
        ...

    async def publish(self, post: PostRequest) -> PublishResult:
        """Create (or schedule) the post."""
        ...
