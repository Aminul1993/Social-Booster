"""Session, CSRF and flash-message primitives.

Session model
-------------
Starlette's ``SessionMiddleware`` keeps a small dict in an **itsdangerous-signed**,
``HttpOnly``, ``SameSite=Lax`` cookie (``Secure`` in production). It holds only:

* ``sid``   - random opaque session id (key for server-side data),
* ``csrf``  - per-session CSRF token,
* ``flash`` - one-shot messages for the next page load.

The Buffer access token is server configuration (``BUFFER_ACCESS_TOKEN``): it
is never placed in the cookie or rendered into a page.
"""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import Request

from app.errors import CSRFError

SESSION_ID_KEY = "sid"
CSRF_KEY = "csrf"
FLASH_KEY = "flash"

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
MAX_FLASH_MESSAGES = 5

FlashLevel = Literal["success", "info", "warning", "danger"]


# --------------------------------------------------------------------------- session
def ensure_session(request: Request) -> str:
    """Return the session id, creating the session (sid + CSRF token) if needed."""
    session = request.session
    sid = session.get(SESSION_ID_KEY)
    if not isinstance(sid, str) or len(sid) < 32:
        sid = secrets.token_urlsafe(32)
        session[SESSION_ID_KEY] = sid
        session[CSRF_KEY] = secrets.token_urlsafe(32)
    elif not session.get(CSRF_KEY):
        session[CSRF_KEY] = secrets.token_urlsafe(32)
    return sid


def get_csrf_token(request: Request) -> str:
    """CSRF token of the current session ('' when there is no session yet)."""
    if "session" not in request.scope:
        return ""
    token = request.session.get(CSRF_KEY)
    return token if isinstance(token, str) else ""


async def verify_csrf(request: Request) -> None:
    """Dependency: reject unsafe requests without a matching ``X-CSRF-Token``.

    Synchronizer-token pattern: the token lives in the signed session and is
    rendered into the page (``hx-headers``); HTMX echoes it on every request.
    A cross-site attacker can neither read the page nor forge the cookie.
    """
    if request.method in SAFE_METHODS:
        return
    expected = get_csrf_token(request)
    provided = request.headers.get(CSRF_HEADER, "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        raise CSRFError()


# ---------------------------------------------------------------------------- flashes
def add_flash(request: Request, level: FlashLevel, message: str) -> None:
    """Queue a toast for the next full page render (survives redirects)."""
    flashes = request.session.get(FLASH_KEY)
    if not isinstance(flashes, list):
        flashes = []
    flashes.append({"level": level, "message": message})
    request.session[FLASH_KEY] = flashes[-MAX_FLASH_MESSAGES:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    flashes = request.session.pop(FLASH_KEY, None)
    if not isinstance(flashes, list):
        return []
    return [
        {"level": str(item.get("level", "info")), "message": str(item.get("message", ""))}
        for item in flashes
        if isinstance(item, dict) and item.get("message")
    ]
