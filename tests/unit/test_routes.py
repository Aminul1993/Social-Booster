"""HTTP-level behaviour of the application shell: pages, security, errors, ops."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import uvicorn

import app.__main__ as entry_module
import app.main as main_module
from app.errors import RateLimitExceededError
from services.errors import StorageError
from tests.conftest import AppFactory
from tests.helpers import (
    AppClient,
    FakeDescriber,
    make_client,
    make_image,
    make_raw_client,
    toast,
)

pytestmark = pytest.mark.usefixtures("mock_http")


# ------------------------------------------------------------------------ index page
class TestIndex:
    async def test_renders_with_security_headers(self, client: AppClient) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        html = response.text
        assert '<meta name="htmx-config"' in html
        assert "vendor/htmx/htmx.min.js?v=1.0.0" in html
        assert "Connect Buffer" in html
        assert "No drafts yet" in html
        headers = response.headers
        assert headers["Cache-Control"] == "no-store"
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Strict-Transport-Security" not in headers  # dev/test: no HSTS
        assert len(headers["X-Request-ID"]) == 32

    async def test_session_cookie_is_signed_httponly_lax(self, app_factory: AppFactory) -> None:
        async with make_client(app_factory()) as client:
            response = await client.http.get("/", headers={"Cookie": ""})
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("mab_session=")
        assert "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        assert "secure" not in cookie.lower()

    async def test_secure_cookie_and_hsts_in_production(self, app_factory: AppFactory) -> None:
        app = app_factory(environment="production", session_secret="p" * 48)
        async with make_client(app) as client:
            response = await client.http.get("/", headers={"Cookie": ""})
            assert "secure" in response.headers["set-cookie"].lower()
            assert "max-age=63072000" in response.headers["Strict-Transport-Security"]
            assert (await client.get("/docs")).status_code == 404

    async def test_configuration_warnings(self, app_factory: AppFactory) -> None:
        app = app_factory(
            ollama_api_key=None,
            ollama_endpoint="https://ollama.com/v1/chat/completions",
            vision_backend="disabled",
            buffer_client_id=None,
        )
        async with make_client(app) as client:
            html = (await client.get("/")).text
        assert "AI copywriting is not configured" in html
        assert "Automatic image description is turned off" in html
        assert "Buffer not configured" in html

    async def test_vision_error_banner(self, app_factory: AppFactory) -> None:
        unconfigured = FakeDescriber()
        unconfigured.configured = False
        async with make_client(app_factory(describer=unconfigured)) as client:
            html = (await client.get("/")).text
        assert "Automatic image description is unavailable" in html

    async def test_request_id_propagation(self, client: AppClient) -> None:
        good = await client.get("/health", headers={"X-Request-ID": "trace-abc-12345"})
        assert good.headers["X-Request-ID"] == "trace-abc-12345"
        bad = await client.get("/health", headers={"X-Request-ID": "<script>"})
        assert bad.headers["X-Request-ID"] != "<script>"

    async def test_api_docs_in_development(self, client: AppClient) -> None:
        assert (await client.get("/docs")).status_code == 200
        schema = (await client.get("/openapi.json")).json()
        assert {"/upload", "/generate", "/buffer/schedule", "/health/ready"} <= set(schema["paths"])


# ---------------------------------------------------------------------------- CSRF
class TestCsrf:
    async def test_missing_token_htmx(self, client: AppClient) -> None:
        response = await client.http.post("/generate", headers={"HX-Request": "true"})
        assert response.status_code == 403
        assert "session could not be verified" in toast(response)["message"]
        assert response.headers["HX-Reswap"] == "none"

    async def test_wrong_token_browser(self, client: AppClient) -> None:
        response = await client.http.post(
            "/buffer/disconnect", headers={"X-CSRF-Token": "forged", "Accept": "text/html"}
        )
        assert response.status_code == 403
        assert "<h1" in response.text
        assert "Forbidden" in response.text

    async def test_token_from_other_session_rejected(self, client: AppClient) -> None:
        other_token = client.csrf
        client.http.cookies.clear()
        await client.http.get("/")
        response = await client.http.post(
            "/buffer/disconnect", headers={"X-CSRF-Token": other_token, "HX-Request": "true"}
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- errors
class TestErrors:
    async def test_404_html_and_json(self, client: AppClient) -> None:
        html = await client.get("/missing")
        assert html.status_code == 404
        assert "does not exist" in html.text
        api = await client.get("/missing", headers={"Accept": "application/json"})
        assert api.json()["detail"] == "The page you are looking for does not exist."
        assert api.json()["request_id"] == api.headers["X-Request-ID"]

    async def test_405(self, client: AppClient) -> None:
        response = await client.get("/upload", headers={"HX-Request": "true"})
        assert response.status_code == 405
        assert toast(response)["message"] == "This action is not allowed here."

    async def test_validation_error_is_a_toast(self, client: AppClient) -> None:
        response = await client.post("/generate", data={"draft_id": "nope"})
        assert response.status_code == 422
        assert toast(response)["message"].startswith("Invalid draft_id")

    async def test_unhandled_exception(
        self, client: AppClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            client.container.drafts,
            "list_for_session",
            AsyncMock(side_effect=RuntimeError("kaboom")),
        )
        page = await client.get("/")
        assert page.status_code == 500
        assert "Reference:" in page.text
        assert page.headers["Content-Security-Policy"]  # still secured
        frag = await client.get("/", headers={"HX-Request": "true"})
        assert frag.status_code == 500
        assert "unexpected error" in toast(frag)["message"]
        api = await client.get("/", headers={"Accept": "application/json"})
        assert api.json()["detail"].startswith("An unexpected error occurred")

    async def test_trusted_hosts(self, app_factory: AppFactory) -> None:
        app = app_factory(allowed_hosts="good.example.com")
        async with make_raw_client(app) as http:
            assert (await http.get("/health")).status_code == 400
            ok = await http.get("/health", headers={"Host": "good.example.com"})
            assert ok.status_code == 200


# ----------------------------------------------------------------------- body limits
class TestBodyLimit:
    async def test_declared_too_large(self, app_factory: AppFactory) -> None:
        app = app_factory(max_request_body_mb=0.001)
        async with make_client(app) as client:
            response = await client.post("/generate", content=b"x" * 5000)
            assert response.status_code == 413
            assert toast(response)["message"] == "The upload is too large."
            plain = await client.http.post("/generate", content=b"x" * 5000)
            assert plain.status_code == 413
            assert "HX-Trigger" not in plain.headers

    async def test_streamed_too_large(self, app_factory: AppFactory) -> None:
        app = app_factory(max_request_body_mb=0.001)

        body = (
            b'--xyz\r\nContent-Disposition: form-data; name="files"; filename="a.png"\r\n'
            b"Content-Type: image/png\r\n\r\n" + b"y" * 5000 + b"\r\n--xyz--\r\n"
        )

        async def chunks() -> AsyncIterator[bytes]:
            # No Content-Length: the limit must be enforced while streaming.
            for start in range(0, len(body), 500):
                yield body[start : start + 500]

        async with make_client(app) as client:
            response = await client.post(
                "/upload",
                content=chunks(),
                headers={"Content-Type": "multipart/form-data; boundary=xyz"},
            )
        assert response.status_code == 413
        assert "too large" in toast(response)["message"]


# ---------------------------------------------------------------------- rate limits
class TestRateLimiting:
    async def test_upload_rate_limited(self, app_factory: AppFactory) -> None:
        app = app_factory(rate_limit_enabled=True, rate_limit_upload="1/minute")
        async with make_client(app) as client:
            first = await client.upload(("a.png", make_image(), "image/png"))
            assert first.status_code == 200
            second = await client.upload(("b.png", make_image(), "image/png"))
            assert second.status_code == 429
            assert int(second.headers["Retry-After"]) >= 1
            assert "Too many requests" in toast(second)["message"]
            metrics = (await client.get("/metrics")).text
            assert 'mab_rate_limited_total{scope="upload"} 1.0' in metrics

    def test_retry_after_floor(self) -> None:
        assert RateLimitExceededError(retry_after=0).retry_after == 1


# ------------------------------------------------------------------------ ops routes
class TestOps:
    async def test_health(self, client: AppClient) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["version"] == "1.0.0"
        assert body["environment"] == "test"

    async def test_ready_ok(self, client: AppClient) -> None:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        checks = response.json()["checks"]
        assert checks["database"]["status"] == "ok"
        assert checks["storage"]["status"] == "ok"
        assert checks["vision"]["status"] == "ok"
        assert checks["ollama"]["status"] == "configured"
        assert checks["buffer"]["status"] == "configured"

    async def test_ready_storage_and_db_failure(
        self, client: AppClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            client.container.storage,
            "check_health",
            AsyncMock(side_effect=StorageError("Upload directory is not writable")),
        )
        monkeypatch.setattr(client.container.database, "ping", AsyncMock(side_effect=OSError()))
        response = await client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["storage"]["detail"] == "Upload directory is not writable"
        assert body["checks"]["database"]["detail"] == "OSError"

    async def test_ready_degraded_when_vision_not_configured(self, app_factory: AppFactory) -> None:
        unconfigured = FakeDescriber()
        unconfigured.configured = False
        async with make_client(app_factory(describer=unconfigured)) as client:
            body = (await client.get("/health/ready")).json()
        assert body["status"] == "degraded"
        assert body["checks"]["vision"]["detail"] == "Image description is not configured."

    async def test_metrics_open(self, client: AppClient) -> None:
        await client.get("/health")
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert 'mab_http_requests_total{method="GET",route="/health",status="200"}' in response.text

    async def test_metrics_token_and_disabled(self, app_factory: AppFactory) -> None:
        async with make_client(app_factory(metrics_token="m-token")) as client:
            assert (await client.get("/metrics")).status_code == 401
            denied = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
            assert denied.status_code == 401
            allowed = await client.get("/metrics", headers={"Authorization": "Bearer m-token"})
            assert allowed.status_code == 200
        async with make_client(app_factory(metrics_enabled=False)) as client:
            assert (await client.get("/metrics")).status_code == 404

    async def test_metrics_multiprocess_mode(
        self, client: AppClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        prom_dir = tmp_path / "prometheus"  # must only contain prometheus .db files
        prom_dir.mkdir()
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(prom_dir))
        response = await client.get("/metrics")
        assert response.status_code == 200

    async def test_static_assets_gzip(self, client: AppClient) -> None:
        response = await client.get("/static/css/style.css", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert "AI Marketing Post Builder" in response.text  # httpx transparently decompresses


# -------------------------------------------------------------------- module wiring
class TestModuleEntryPoints:
    def test_lazy_app_attribute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = object()
        monkeypatch.setattr(main_module, "_app", None)
        monkeypatch.setattr(main_module, "create_app", lambda: sentinel)
        assert main_module.app is sentinel
        assert main_module.app is sentinel  # cached
        with pytest.raises(AttributeError):
            _ = main_module.does_not_exist

    def test_python_dash_m_entry_point(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
        entry_module.main()
        ((args, kwargs),) = calls
        assert args == ("app.main:app",)
        assert kwargs["proxy_headers"] is True
        assert kwargs["log_config"] is None
