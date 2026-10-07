"""Composition root lifecycle: startup, background maintenance and retention."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.container import build_container
from app.models import Draft
from services.publishing import OAuthToken
from tests.conftest import SettingsFactory
from tests.helpers import FakeClassifier, make_image


async def test_run_maintenance_purges_expired_data(settings_factory: SettingsFactory) -> None:
    container = build_container(
        settings_factory(draft_retention_hours=1, session_max_age_seconds=3600),
        classifier_factory=FakeClassifier,
    )
    await container.database.connect()
    try:
        stored = await container.storage.save(
            make_image(), extension="png", content_type="image/png"
        )
        old = datetime.now(UTC) - timedelta(hours=5)
        draft = Draft(
            session_id="s",
            image_key=stored.key,
            original_filename="old.png",
            content_type="image/png",
            width=1,
            height=1,
            size_bytes=1,
            created_at=old,
            updated_at=old,
        )
        await container.drafts._repo.add(draft)
        await container.tokens.save("s", "buffer", OAuthToken(access_token="t"))
        await container.database.conn.execute(
            "UPDATE oauth_tokens SET updated_at = ?", (old.isoformat(),)
        )
        await container.database.conn.commit()

        await container.run_maintenance()

        assert await container.drafts._repo.count_for_session("s") == 0
        assert not await container.storage.exists(stored.key)
        assert await container.tokens.get("s", "buffer") is None
    finally:
        await container.aclose()


async def test_lifecycle_starts_and_stops_background_tasks(
    settings_factory: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = build_container(
        settings_factory(draft_retention_hours=24), classifier_factory=FakeClassifier
    )
    maintenance = AsyncMock(side_effect=[RuntimeError("disk on fire"), None])
    monkeypatch.setattr(container, "run_maintenance", maintenance)
    monkeypatch.setattr("app.container.MAINTENANCE_INTERVAL_SECONDS", 0.01)

    await container.start()
    for _ in range(50):
        if maintenance.await_count >= 2 and container.vision.ready:
            break
        await asyncio.sleep(0.01)
    assert maintenance.await_count >= 2  # failure was logged, loop kept running
    assert container.vision.ready
    assert container.uptime_seconds >= 0
    await container.aclose()
    assert container._tasks == []


async def test_no_maintenance_task_when_retention_disabled(
    settings_factory: SettingsFactory,
) -> None:
    container = build_container(settings_factory(draft_retention_hours=0))
    await container.start()
    try:
        assert [task.get_name() for task in container._tasks] == ["vision-load"]
    finally:
        await container.aclose()
