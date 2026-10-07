from __future__ import annotations

import json
import logging
import sys

import pytest

from app.logging_config import (
    ConsoleFormatter,
    JsonFormatter,
    configure_logging,
    redact,
    request_id_ctx,
)


def record(message: str, *args: object, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, args, None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def-123", "Authorization: Bearer [REDACTED]"),
        ("GET /cb?code=xyz&state=1", "GET /cb?code=[REDACTED]&state=1"),
        ('{"access_token": "1/abc"}', '{"access_token": "[REDACTED]"}'),
        ("status_code=200", "status_code=200"),
    ],
)
def test_redact(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_json_formatter_includes_context_extras_and_exception() -> None:
    token = request_id_ctx.set("req-123")
    try:
        rec = record("hello %s", "world", draft_id="d1", secret="Bearer xyz")
        try:
            raise ValueError("boom")
        except ValueError:
            rec.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_ctx.reset(token)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["request_id"] == "req-123"
    assert payload["draft_id"] == "d1"
    assert payload["secret"] == "Bearer [REDACTED]"
    assert "ValueError: boom" in payload["exception"]
    assert payload["timestamp"].endswith("+00:00")


def test_console_formatter_appends_extras() -> None:
    token = request_id_ctx.set("rid")
    try:
        line = ConsoleFormatter().format(record("done", status=200))
    finally:
        request_id_ctx.reset(token)
    assert "INFO" in line
    assert "done [request_id=rid status=200]" in line
    assert ConsoleFormatter().format(record("plain")).endswith("plain")


@pytest.mark.parametrize(
    ("fmt", "formatter"), [("json", JsonFormatter), ("console", ConsoleFormatter)]
)
def test_configure_logging(fmt: str, formatter: type) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("DEBUG", fmt)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, formatter)
        assert root.level == logging.DEBUG
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("uvicorn.error").propagate
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
