"""Structured logging.

* ``LOG_FORMAT=json`` (default) emits one JSON object per line - ready for
  Loki, CloudWatch, Datadog, ELK.
* ``LOG_FORMAT=console`` emits human-friendly lines for local development.

Every record carries the current ``request_id`` (set by
:class:`app.middleware.RequestContextMiddleware`) and any ``extra={...}``
fields. A redaction filter scrubs bearer tokens, OAuth codes and secrets as a
last line of defence.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED_ATTRS = frozenset(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of the message
}

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|client_secret|code|api_key|password|token)"
            r"(=|\":\s*\"|':\s*')([^&\s\"']+)"
        ),
        r"\1\2[REDACTED]",
    ),
)


def redact(text: str) -> str:
    """Mask credentials that may have slipped into a log message."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _RESERVED_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON documents."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in _extras(record).items():
            payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Readable single-line format with ``key=value`` extras."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = redact(super().format(record))
        extras = _extras(record)
        request_id = request_id_ctx.get()
        if request_id:
            extras = {"request_id": request_id, **extras}
        if extras:
            rendered = " ".join(f"{key}={value}" for key, value in extras.items())
            first, newline, rest = line.partition("\n")
            line = f"{first} [{redact(rendered)}]{newline}{rest}"
        return line


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Route server loggers through the same handler; our middleware writes the
    # access log, so uvicorn's own access logger is silenced.
    for name in ("uvicorn", "uvicorn.error", "gunicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx logs full request URLs at INFO; keep it quiet.
    for noisy in ("httpx", "httpcore", "PIL", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
