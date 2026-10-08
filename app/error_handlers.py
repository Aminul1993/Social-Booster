"""Translate exceptions into responses suited to the caller.

* HTMX request  -> empty body, ``HX-Trigger`` toast, ``HX-Reswap: none``
  (the page keeps its current state and shows a notification).
* JSON client   -> ``{"detail": ..., "request_id": ...}``.
* Browser page  -> rendered ``errors/error.html``.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, ToastLevel, is_htmx, toast_header, wants_json
from app.logging_config import request_id_ctx
from app.templating import templates
from services.errors import (
    OllamaAuthError,
    OllamaError,
    PublisherAPIError,
    PublisherAuthError,
    PublisherError,
    ServiceError,
    ServiceNotConfiguredError,
    StorageError,
    VisionError,
)

logger = logging.getLogger(__name__)


def error_response(
    request: Request,
    *,
    status_code: int,
    message: str,
    headers: dict[str, str] | None = None,
    level: ToastLevel = "danger",
) -> Response:
    response_headers = dict(headers or {})
    if is_htmx(request):
        response_headers.update(toast_header(level, message))
        response_headers["HX-Reswap"] = "none"
        return Response(status_code=status_code, headers=response_headers)
    if wants_json(request):
        return JSONResponse(
            {"detail": message, "request_id": request_id_ctx.get()},
            status_code=status_code,
            headers=response_headers,
        )
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    return templates.TemplateResponse(
        request,
        "errors/error.html",
        {"status_code": status_code, "title": title, "message": message},
        status_code=status_code,
        headers=response_headers,
    )


def map_service_error(exc: ServiceError) -> tuple[int, str]:
    """HTTP status and user-facing message for a service-layer failure."""
    if isinstance(exc, ServiceNotConfiguredError):
        return 503, exc.message
    if isinstance(exc, OllamaAuthError):
        return 502, "The AI service rejected the configured API key. Check OLLAMA_API_KEY."
    if isinstance(exc, OllamaError):
        status = 503 if exc.retryable else 502
        return status, f"{exc.message} Please try again."
    if isinstance(exc, PublisherAuthError):
        return 502, exc.message
    if isinstance(exc, PublisherAPIError):
        return 502, exc.message
    if isinstance(exc, PublisherError):
        return 503, exc.message
    if isinstance(exc, VisionError):
        return 503, exc.message
    if isinstance(exc, StorageError):
        return 500, "The file could not be stored. Please try again."
    return 502, exc.message


async def handle_app_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, AppError):  # pragma: no cover - guaranteed by registration
        raise exc
    if exc.status_code >= 500:
        logger.error("Request failed: %s", exc.message)
    return error_response(
        request, status_code=exc.status_code, message=exc.message, headers=exc.headers
    )


async def handle_service_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ServiceError):  # pragma: no cover - guaranteed by registration
        raise exc
    status_code, message = map_service_error(exc)
    logger.warning(
        "Service error",
        extra={
            "service": exc.service,
            "error_type": type(exc).__name__,
            "error": exc.message,
            "upstream_status": exc.status_code,
        },
    )
    return error_response(request, status_code=status_code, message=message)


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - guaranteed by registration
        raise exc
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", ())[1:]) or "request"
    message = f"Invalid {field}: {first.get('msg', 'invalid value')}."
    return error_response(request, status_code=422, message=message)


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - guaranteed by registration
        raise exc
    messages = {
        404: "The page you are looking for does not exist.",
        405: "This action is not allowed here.",
        413: "The upload is too large.",
    }
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code not in messages else None
    message = detail or messages.get(exc.status_code, "The request could not be processed.")
    return error_response(
        request,
        status_code=exc.status_code,
        message=message,
        headers=dict(exc.headers or {}),
    )


def render_unhandled_error(request: Request) -> Response:
    """Used by the request middleware for exceptions nothing else handled."""
    reference = request_id_ctx.get() or "n/a"
    message = f"An unexpected error occurred. Reference: {reference}."
    try:
        return error_response(request, status_code=500, message=message)
    except Exception:  # pragma: no cover - template failure inside the error path
        logger.exception("Error page rendering failed")
        return Response(message, status_code=500, media_type="text/plain")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(ServiceError, handle_service_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
