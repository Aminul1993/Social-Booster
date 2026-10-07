"""Pure-ASGI middleware (no BaseHTTPMiddleware: streaming-safe and faster).

Order (outermost first), configured in :func:`app.main.create_app`:

1. :class:`SecurityHeadersMiddleware` - CSP, HSTS, nosniff, frame denial...
2. :class:`RequestContextMiddleware`  - request id, access log, metrics,
   last-resort error page.
3. :class:`BodySizeLimitMiddleware`   - rejects oversized bodies early (413).
4. Starlette ``TrustedHostMiddleware`` and ``SessionMiddleware``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import toast_header
from app.logging_config import request_id_ctx
from app.metrics import AppMetrics

access_logger = logging.getLogger("app.access")
logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
_QUIET_PATHS = ("/health", "/metrics", "/static/", "/uploads/", "/favicon.ico")


def build_csp(*, extra_img_sources: tuple[str, ...] = ("https:",)) -> str:
    """Strict CSP: only same-origin scripts/styles, no inline code, no framing."""
    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'", "data:", "blob:", *extra_img_sources],
        "font-src": ["'self'", "data:"],
        "connect-src": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'self'"],
        "object-src": ["'none'"],
    }
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool, csp: str | None = None) -> None:
        self.app = app
        self.headers: dict[str, str] = {
            "Content-Security-Policy": csp or build_csp(),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-site",
        }
        if hsts:
            self.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_upload = scope.get("path", "").startswith("/uploads/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    if is_upload and name == "Cross-Origin-Resource-Policy":
                        # Uploaded images must be fetchable by Buffer / social networks.
                        value = "cross-origin"
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestContextMiddleware:
    """Request id propagation, structured access log and HTTP metrics."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: AppMetrics,
        error_renderer: Callable[[Request], Response],
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.error_renderer = error_renderer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("Unhandled exception", extra={"path": scope.get("path")})
            if response_started:
                raise
            response = self.error_renderer(Request(scope))
            await response(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            route = self._route_label(scope)
            method = scope.get("method", "GET")
            self.metrics.http_requests.labels(
                method=method, route=route, status=str(status_code)
            ).inc()
            self.metrics.http_latency.labels(method=method, route=route).observe(duration)
            path = scope.get("path", "")
            level = logging.DEBUG if path.startswith(_QUIET_PATHS) else logging.INFO
            if status_code >= 500:
                level = logging.ERROR
            access_logger.log(
                level,
                "%s %s %s",
                method,
                path,
                status_code,
                extra={
                    "method": method,
                    "path": path,
                    "route": route,
                    "status": status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "client": (scope.get("client") or ("-",))[0],
                    "htmx": Headers(scope=scope).get("hx-request") == "true",
                },
            )
            request_id_ctx.reset(token)

    @staticmethod
    def _route_label(scope: Scope) -> str:
        route = scope.get("route")
        path_template = getattr(route, "path", None)
        if isinstance(path_template, str):
            return path_template
        path = scope.get("path", "")
        for prefix in ("/static", "/uploads"):
            if path.startswith(prefix + "/"):
                return prefix
        return "unmatched"


class BodySizeLimitMiddleware:
    """Reject request bodies above ``max_bytes`` (declared or streamed)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            extra: dict[str, str] = {}
            if headers.get("hx-request") == "true":
                extra = {**toast_header("danger", "The upload is too large."), "HX-Reswap": "none"}
            response = Response("Request body too large.", status_code=413, headers=extra)
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Request body too large.")
            return message

        await self.app(scope, limited_receive, send)
