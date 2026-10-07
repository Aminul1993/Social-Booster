"""View models and render helpers shared by the HTML routes."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.accounts import PublisherStatus
from app.config import Settings
from app.models import Draft, ScheduleForm
from app.templating import templates
from services.publishing import PublishMode
from services.storage import Storage


@dataclass(frozen=True, slots=True)
class CardView:
    """Everything ``_card.html`` renders for one draft.

    Form values default to the stored draft but are overridden by submitted
    values when a validation error re-renders the card, so nothing the user
    typed is lost.
    """

    draft: Draft
    image_url: str
    caption: str
    hashtags: str
    keywords: str
    tone: str
    selected_profiles: frozenset[str]
    mode: str
    scheduled_for: str
    timezone: str
    errors: dict[str, str] = field(default_factory=dict)
    notice: str | None = None
    notice_level: str = "success"
    focus: str | None = None


def image_url(request: Request, storage: Storage, draft: Draft) -> str:
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}{storage.public_path(draft.image_key)}"


def build_card(
    request: Request,
    storage: Storage,
    draft: Draft,
    *,
    schedule_form: ScheduleForm | None = None,
    keywords: str | None = None,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    notice_level: str = "success",
    focus: str | None = None,
) -> CardView:
    publish = draft.publish
    if schedule_form is not None:
        selected = frozenset(schedule_form.profile_ids)
        mode = schedule_form.mode
        scheduled_for = schedule_form.scheduled_for
        timezone = schedule_form.timezone
        caption, hashtags = schedule_form.caption, schedule_form.hashtags
    else:
        selected = frozenset(publish.profile_ids) if publish else frozenset()
        mode = PublishMode.SCHEDULE.value
        scheduled_for = ""
        timezone = ""
        caption, hashtags = draft.caption, draft.hashtags_text
    return CardView(
        draft=draft,
        image_url=image_url(request, storage, draft),
        caption=caption,
        hashtags=hashtags,
        keywords=draft.keywords_text if keywords is None else keywords,
        tone=draft.tone.value,
        selected_profiles=selected,
        mode=mode,
        scheduled_for=scheduled_for,
        timezone=timezone,
        errors=errors or {},
        notice=notice,
        notice_level=notice_level,
        focus=focus,
    )


def render_cards(
    request: Request,
    cards: Sequence[CardView],
    publisher: PublisherStatus,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    context: dict[str, Any] = {"cards": cards, "publisher": publisher}
    return templates.TemplateResponse(
        request, "partials/_cards.html", context, status_code=status_code, headers=headers
    )


def render_card(
    request: Request,
    card: CardView,
    publisher: PublisherStatus,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_card.html",
        {"card": card, "publisher": publisher},
        status_code=status_code,
        headers=headers,
    )


def absolute_url(request: Request, settings: Settings, path: str) -> str:
    """Public absolute URL for ``path``.

    ``PUBLIC_BASE_URL`` (which must include any sub-path prefix) wins over the
    request's own base URL, which already contains the ASGI ``root_path``.
    """
    if path.startswith(("http://", "https://")):
        return path
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}{path}"


def is_publicly_reachable(url: str) -> bool:
    """Heuristic: can a third party (Buffer) fetch this URL?"""
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host
