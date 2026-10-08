from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request

from app.errors import CSRFError
from app.security import (
    CSRF_KEY,
    SESSION_ID_KEY,
    add_flash,
    ensure_session,
    get_csrf_token,
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
