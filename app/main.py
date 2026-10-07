"""FastAPI entry point.

Run locally:      uvicorn app.main:app --reload
Run in production: gunicorn -c gunicorn.conf.py app.main:app

``app`` is created lazily on first access (PEP 562 module ``__getattr__``) so
tests and tools can import :func:`create_app` without building the default
application from the environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.config import Settings, get_settings
from app.container import build_container
from app.error_handlers import register_exception_handlers, render_unhandled_error
from app.logging_config import configure_logging
from app.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.routes import buffer, drafts, ops, pages
from app.templating import STATIC_DIR
from services.vision import ImageDescriber

DESCRIPTION = """
Upload photos, let a vision model describe them and suggest keywords, have
Ollama Cloud write a caption and hashtags, edit the copy and schedule the post
on Buffer.

The UI endpoints return **HTML fragments for HTMX**. Every state-changing
request requires the `X-CSRF-Token` header (rendered into the page).
"""


def create_app(
    settings: Settings | None = None,
    *,
    describer: ImageDescriber | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Application factory."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    container = build_container(
        settings,
        describer=describer,
        http_transport=http_transport,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await container.start()
        try:
            yield
        finally:
            await container.aclose()

    docs = settings.docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.container = container
    register_exception_handlers(app)

    for module in (pages, drafts, buffer, ops):
        app.include_router(module.router)

    app.mount(
        "/static",
        GZipMiddleware(StaticFiles(directory=STATIC_DIR), minimum_size=1024),
        name="static",
    )
    app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

    # Middleware: the last one added is the outermost.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_value,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    if settings.allowed_hosts != ["*"]:
        # Loopback stays allowed so the container health check keeps working.
        hosts = [*settings.allowed_hosts, "localhost", "127.0.0.1"]
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(
        RequestContextMiddleware,
        metrics=container.metrics,
        error_renderer=render_unhandled_error,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=not _settings.is_production,
        proxy_headers=True,
    )
