"""Pytest fixtures: isolated settings, app instances and an HTMX test client."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from services.vision import ImageDescriber
from tests.helpers import (
    BUFFER_API_URL,
    BUFFER_OAUTH_URL,
    BUFFER_TOKEN_URL,
    OLLAMA_URL,
    AppClient,
    FakeDescriber,
    make_client,
)

SettingsFactory = Callable[..., Settings]


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure the developer's environment never leaks into tests."""
    fields = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in fields:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)


@pytest.fixture
def settings_factory(tmp_path: Path) -> SettingsFactory:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "_env_file": None,
            "environment": "test",
            "session_secret": "test-session-secret-" + "x" * 40,
            "database_path": tmp_path / "app.db",
            "upload_dir": tmp_path / "uploads",
            "log_format": "console",
            "log_level": "WARNING",
            "ollama_api_key": "test-ollama-key",
            "ollama_endpoint": OLLAMA_URL,
            "ollama_model": "test-model",
            "ollama_max_retries": 2,
            "buffer_client_id": "client-id",
            "buffer_client_secret": "client-secret",
            "buffer_redirect_uri": "http://testserver/buffer/callback",
            "buffer_oauth_url": BUFFER_OAUTH_URL,
            "buffer_token_url": BUFFER_TOKEN_URL,
            "buffer_api_url": BUFFER_API_URL,
            "buffer_max_retries": 1,
            "rate_limit_enabled": False,
            "draft_retention_hours": 0,
            "public_base_url": "https://social.example.com",
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    return settings_factory()


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def app_factory(settings_factory: SettingsFactory) -> AppFactory:
    def factory(
        settings: Settings | None = None,
        describer: ImageDescriber | None = None,
        **overrides: Any,
    ) -> FastAPI:
        # A deterministic fake vision model unless a test passes its own describer.
        return create_app(
            settings or settings_factory(**overrides),
            describer=describer or FakeDescriber(),
        )

    return factory


@pytest.fixture
def app(app_factory: AppFactory) -> FastAPI:
    return app_factory()


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    """Intercept outbound HTTP (Ollama / Buffer). Unmatched calls fail loudly."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
async def client(app: FastAPI, mock_http: respx.MockRouter) -> AsyncIterator[AppClient]:
    async with make_client(app) as app_client:
        yield app_client
