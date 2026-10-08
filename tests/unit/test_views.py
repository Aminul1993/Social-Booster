"""Small pure helpers used by the HTML layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.config import Settings
from app.error_handlers import error_response, map_service_error
from app.templating import _filesize, _format_utc, _iso, static_url
from app.views import absolute_url, is_publicly_reachable
from services.errors import (
    OllamaAuthError,
    OllamaError,
    OllamaNotConfiguredError,
    PublisherAPIError,
    PublisherAuthError,
    PublisherError,
    ServiceError,
    ServiceNotConfiguredError,
    StorageError,
    VisionError,
)


def request(root_path: str = "", headers: dict[str, str] | None = None) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "server": ("app.example.com", 443),
        "path": "/",
        "root_path": root_path,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("error", "status", "fragment"),
    [
        (ServiceNotConfiguredError("Buffer is not configured"), 503, "not configured"),
        (OllamaNotConfiguredError("set OLLAMA_API_KEY"), 503, "OLLAMA_API_KEY"),
        (OllamaAuthError("bad key"), 502, "OLLAMA_API_KEY"),
        (OllamaError("busy", retryable=True), 503, "Please try again"),
        (OllamaError("bad request"), 502, "bad request"),
        (PublisherAuthError("Check BUFFER_ACCESS_TOKEN"), 502, "BUFFER_ACCESS_TOKEN"),
        (PublisherAPIError("rejected"), 502, "rejected"),
        (PublisherError("down"), 503, "down"),
        (VisionError("no model"), 503, "no model"),
        (StorageError("disk"), 500, "could not be stored"),
        (ServiceError("other"), 502, "other"),
    ],
)
def test_map_service_error(error: ServiceError, status: int, fragment: str) -> None:
    mapped_status, message = map_service_error(error)
    assert mapped_status == status
    assert fragment in message


def test_error_response_for_non_standard_status() -> None:
    api = error_response(
        request(headers={"accept": "application/json"}), status_code=599, message="weird"
    )
    assert api.status_code == 599
    page_request = request()
    page_request.scope["app"] = FastAPI()
    page = error_response(page_request, status_code=599, message="weird")
    assert page.status_code == 599
    assert b'<h1 class="h3 mb-3">Error</h1>' in page.body


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://social.example.com/uploads/a.png", True),
        ("http://localhost:8000/uploads/a.png", False),
        ("http://127.0.0.1/uploads/a.png", False),
        ("http://10.0.0.5/uploads/a.png", False),
        ("http://8.8.8.8/uploads/a.png", True),
        ("http://myhost.local/x", False),
        ("http://testserver/x", False),
        ("not a url", False),
    ],
)
def test_is_publicly_reachable(url: str, expected: bool) -> None:
    assert is_publicly_reachable(url) is expected


def test_absolute_url() -> None:
    settings = Settings(_env_file=None, public_base_url="https://cdn.example.com/app")
    assert absolute_url(request(), settings, "/uploads/a.png") == (
        "https://cdn.example.com/app/uploads/a.png"
    )
    assert absolute_url(request(), settings, "https://x/y") == "https://x/y"
    plain = Settings(_env_file=None)
    assert absolute_url(request(root_path="/mab"), plain, "/uploads/a.png") == (
        "https://app.example.com/mab/uploads/a.png"
    )


def test_template_filters_and_static_url() -> None:
    assert _filesize(512) == "512 B"
    assert _filesize(2048) == "2 KB"
    assert _filesize(3 * 1024 * 1024) == "3.0 MB"
    when = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)
    assert _format_utc(when) == "2026-03-04 05:06 UTC"
    assert _format_utc(None) == ""
    assert _iso(when) == "2026-03-04T05:06:00+00:00"
    assert _iso(None) == ""
    assert static_url(request(root_path="/mab/"), "/css/x.css").startswith(
        "/mab/static/css/x.css?v="
    )
