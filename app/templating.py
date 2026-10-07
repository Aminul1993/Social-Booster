"""Jinja2 environment shared by pages and HTMX fragments."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import BASE_DIR
from app.logging_config import request_id_ctx
from app.security import get_csrf_token
from services.prompts import Tone
from services.publishing import PublishMode

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

#: HTMX runtime configuration (rendered into <meta name="htmx-config">).
#: - eval/script tags disabled so the strict Content-Security-Policy holds;
#: - indicator styles come from style.css (no inline <style> injection);
#: - 422 responses are swapped so validation errors render inline.
HTMX_CONFIG: dict[str, Any] = {
    "allowEval": False,
    "allowScriptTags": False,
    "includeIndicatorStyles": False,
    "selfRequestsOnly": True,
    "historyCacheSize": 0,
    # Never copy inline ``style`` attributes while settling: the CSP forbids them.
    "attributesToSettle": ["class", "width", "height"],
    "responseHandling": [
        {"code": "204", "swap": False},
        {"code": "422", "swap": True, "error": False},
        {"code": "[23]..", "swap": True},
        {"code": "[45]..", "swap": False, "error": True},
    ],
}


def static_url(request: Request, path: str) -> str:
    """Root-path aware, cache-busted URL for a file in ``static/``."""
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}/static/{path.lstrip('/')}?v={__version__}"


def _common_context(request: Request) -> dict[str, Any]:
    container = getattr(request.app.state, "container", None)
    settings = getattr(container, "settings", None)
    return {
        "app_name": getattr(settings, "app_name", "AI Marketing Post Builder"),
        "app_version": __version__,
        "csrf_token": get_csrf_token(request),
        "htmx_config": HTMX_CONFIG,
        "static_url": partial(static_url, request),
        "request_id": request_id_ctx.get(),
    }


def _format_utc(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).strftime(fmt)


def _filesize(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _iso(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value else ""


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_common_context])
templates.env.filters["utc"] = _format_utc
templates.env.filters["iso"] = _iso
templates.env.filters["filesize"] = _filesize
templates.env.globals["tones"] = list(Tone)
templates.env.globals["publish_modes"] = list(PublishMode)
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True
