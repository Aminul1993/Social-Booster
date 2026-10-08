"""Application exceptions and HTMX feedback helpers.

Route code raises these; :mod:`app.error_handlers` turns them into responses:
an HTMX toast (``HX-Trigger``) for in-page requests, JSON for API clients and
an HTML error page for full-page navigations.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import Request

ToastLevel = Literal["success", "info", "warning", "danger"]
TOAST_EVENT = "app:toast"


class AppError(Exception):
    """Base class for errors with a user-safe message and an HTTP status."""

    status_code: int = 400
    default_message: str = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None, *, headers: dict[str, str] | None = None):
        self.message = message or self.default_message
        self.headers = headers or {}
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    default_message = "The request was invalid."


class NotFoundError(AppError):
    status_code = 404
    default_message = "That item no longer exists."


class CSRFError(AppError):
    status_code = 403
    default_message = (
        "Your session could not be verified (it may have expired). Reload the page and try again."
    )


class ServiceUnavailableError(AppError):
    status_code = 503
    default_message = "This feature is temporarily unavailable."


class UpstreamError(AppError):
    status_code = 502
    default_message = "An external service failed. Please try again."


class RateLimitExceededError(AppError):
    status_code = 429
    default_message = "Too many requests. Please slow down and try again shortly."

    def __init__(self, *, retry_after: int) -> None:
        self.retry_after = max(retry_after, 1)
        super().__init__(
            f"Too many requests. Please try again in {self.retry_after} seconds.",
            headers={"Retry-After": str(self.retry_after)},
        )


def is_htmx(request: Request) -> bool:
    """Whether the request was issued by HTMX (expects an HTML fragment)."""
    return request.headers.get("HX-Request") == "true"


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def toast_header(level: ToastLevel, message: str) -> dict[str, str]:
    """``HX-Trigger`` header that makes the front-end show a toast.

    ``ensure_ascii`` keeps the header latin-1 safe for any message language.
    """
    payload = {TOAST_EVENT: {"level": level, "message": message}}
    return {"HX-Trigger": json.dumps(payload, ensure_ascii=True)}
