from __future__ import annotations

import time
from typing import Any

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from app.errors import CSRFError, OAuthStateError
from app.security import (
    CSRF_KEY,
    OAUTH_STATE_KEY,
    OAUTH_STATE_TTL_SECONDS,
    SESSION_ID_KEY,
    TokenCipher,
    TokenDecryptionError,
    add_flash,
    consume_oauth_state,
    ensure_session,
    get_csrf_token,
    issue_oauth_state,
    pop_flashes,
    verify_csrf,
)


def make_request(
    *,
    method: str = "GET",
    session: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    if session is not None:
        scope["session"] = session
    return Request(scope)


class TestSession:
    def test_ensure_session_creates_and_reuses(self) -> None:
        request = make_request(session={})
        sid = ensure_session(request)
        assert len(sid) >= 32
        assert request.session[SESSION_ID_KEY] == sid
        token = request.session[CSRF_KEY]
        assert ensure_session(request) == sid
        assert request.session[CSRF_KEY] == token

    def test_ensure_session_replaces_bogus_sid_and_fills_missing_csrf(self) -> None:
        request = make_request(session={SESSION_ID_KEY: "short"})
        assert ensure_session(request) != "short"

        valid = "s" * 40
        request = make_request(session={SESSION_ID_KEY: valid})
        assert ensure_session(request) == valid
        assert request.session[CSRF_KEY]

    def test_get_csrf_token_without_session(self) -> None:
        assert get_csrf_token(make_request()) == ""
        assert get_csrf_token(make_request(session={CSRF_KEY: 123})) == ""


class TestCsrf:
    async def test_safe_methods_pass(self) -> None:
        await verify_csrf(make_request(method="GET", session={}))

    async def test_valid_token(self) -> None:
        request = make_request(
            method="POST", session={CSRF_KEY: "tok"}, headers={"X-CSRF-Token": "tok"}
        )
        await verify_csrf(request)

    @pytest.mark.parametrize(
        ("session", "headers"),
        [
            ({CSRF_KEY: "tok"}, {}),
            ({CSRF_KEY: "tok"}, {"X-CSRF-Token": "other"}),
            ({}, {"X-CSRF-Token": "tok"}),
        ],
    )
    async def test_rejected(self, session: dict[str, Any], headers: dict[str, str]) -> None:
        with pytest.raises(CSRFError):
            await verify_csrf(make_request(method="POST", session=session, headers=headers))


class TestOAuthState:
    def test_roundtrip_is_single_use(self) -> None:
        request = make_request(session={})
        state = issue_oauth_state(request)
        consume_oauth_state(request, state)
        assert OAUTH_STATE_KEY not in request.session
        with pytest.raises(OAuthStateError):
            consume_oauth_state(request, state)

    def test_mismatch(self) -> None:
        request = make_request(session={})
        issue_oauth_state(request)
        with pytest.raises(OAuthStateError):
            consume_oauth_state(request, "forged")

    def test_missing_received_state(self) -> None:
        request = make_request(session={})
        issue_oauth_state(request)
        with pytest.raises(OAuthStateError):
            consume_oauth_state(request, None)

    def test_expired(self) -> None:
        issued = int(time.time()) - OAUTH_STATE_TTL_SECONDS - 5
        request = make_request(session={OAUTH_STATE_KEY: {"value": "s", "issued_at": issued}})
        with pytest.raises(OAuthStateError, match="expired"):
            consume_oauth_state(request, "s")

    @pytest.mark.parametrize(
        "stored", ["string", {"value": 1, "issued_at": 1}, {"value": "s", "issued_at": "x"}]
    )
    def test_malformed_stored_state(self, stored: object) -> None:
        request = make_request(session={OAUTH_STATE_KEY: stored})
        with pytest.raises(OAuthStateError):
            consume_oauth_state(request, "s")


class TestFlash:
    def test_queue_and_pop(self) -> None:
        request = make_request(session={})
        for i in range(7):
            add_flash(request, "info", f"m{i}")
        flashes = pop_flashes(request)
        assert [f["message"] for f in flashes] == ["m2", "m3", "m4", "m5", "m6"]
        assert pop_flashes(request) == []

    def test_ignores_malformed(self) -> None:
        request = make_request(session={"flash": [{"message": ""}, "x", {"message": "ok"}]})
        assert pop_flashes(request) == [{"level": "info", "message": "ok"}]
        request = make_request(session={"flash": "garbage"})
        add_flash(request, "success", "fresh")
        assert pop_flashes(request) == [{"level": "success", "message": "fresh"}]


class TestTokenCipher:
    def test_derived_key_roundtrip_and_determinism(self) -> None:
        one = TokenCipher.from_secrets(session_secret="a" * 40)
        two = TokenCipher.from_secrets(session_secret="a" * 40)
        ciphertext = one.encrypt("token-value")
        assert b"token-value" not in ciphertext
        assert two.decrypt(ciphertext) == "token-value"

    def test_explicit_key_and_wrong_key(self) -> None:
        key = Fernet.generate_key().decode()
        cipher = TokenCipher.from_secrets(session_secret="a" * 40, explicit_key=key)
        other = TokenCipher.from_secrets(session_secret="a" * 40)
        with pytest.raises(TokenDecryptionError):
            other.decrypt(cipher.encrypt("x"))
        with pytest.raises(TokenDecryptionError):
            cipher.decrypt(b"not-a-token")
