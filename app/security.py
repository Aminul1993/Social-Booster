"""Session, CSRF, OAuth-state and token-encryption primitives.

Session model
-------------
Starlette's ``SessionMiddleware`` keeps a small dict in an **itsdangerous-signed**,
``HttpOnly``, ``SameSite=Lax`` cookie (``Secure`` in production). It holds only:

* ``sid``   - random opaque session id (key for server-side data),
* ``csrf``  - per-session CSRF token,
* ``oauth_state`` - pending OAuth state + issue time,
* ``flash`` - one-shot messages for the next page load.

OAuth access tokens are **never** placed in the cookie: they are encrypted
with Fernet (AES-128-CBC + HMAC-SHA256) and stored server-side keyed by ``sid``.
"""

from __future__ import annotations

import base64
import secrets
import time
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Request

from app.errors import CSRFError, OAuthStateError

SESSION_ID_KEY = "sid"
CSRF_KEY = "csrf"
OAUTH_STATE_KEY = "oauth_state"
FLASH_KEY = "flash"

CSRF_HEADER = "X-CSRF-Token"
OAUTH_STATE_TTL_SECONDS = 600
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


# ------------------------------------------------------------------------ OAuth state
def issue_oauth_state(request: Request) -> str:
    """Create a single-use OAuth ``state`` bound to this session."""
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_KEY] = {"value": state, "issued_at": int(time.time())}
    return state


def consume_oauth_state(request: Request, received: str | None) -> None:
    """Validate the ``state`` returned by the provider; always single-use.

    Raises:
        OAuthStateError: missing, expired or mismatching state (CSRF / replay).
    """
    stored: Any = request.session.pop(OAUTH_STATE_KEY, None)
    if not isinstance(stored, dict) or not received:
        raise OAuthStateError()
    value = stored.get("value")
    issued_at = stored.get("issued_at")
    if not isinstance(value, str) or not isinstance(issued_at, int):
        raise OAuthStateError()
    if time.time() - issued_at > OAUTH_STATE_TTL_SECONDS:
        raise OAuthStateError("The authorization request expired. Please connect again.")
    if not secrets.compare_digest(value, received):
        raise OAuthStateError()


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


# ------------------------------------------------------------------- token encryption
class TokenDecryptionError(Exception):
    """Stored ciphertext could not be decrypted (key rotated or data corrupted)."""


class TokenCipher:
    """Authenticated encryption for OAuth tokens at rest."""

    _HKDF_SALT = b"marketing-ai-builder/token-store/v1"
    _HKDF_INFO = b"oauth-token-encryption"

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_secrets(cls, *, session_secret: str, explicit_key: str | None = None) -> TokenCipher:
        """Use ``TOKEN_ENCRYPTION_KEY`` if given, else derive one from the session secret.

        A dedicated key lets you rotate ``SESSION_SECRET`` (logging everyone out)
        without losing stored Buffer connections.
        """
        if explicit_key:
            return cls(explicit_key.encode())
        derived = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=cls._HKDF_SALT, info=cls._HKDF_INFO
        ).derive(session_secret.encode())
        return cls(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise TokenDecryptionError("Stored token could not be decrypted") from exc
