from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.models import Draft, DraftStatus, Label, PublishRecord
from app.repositories import DraftRepository, TokenRepository
from app.security import TokenCipher
from services.publishing import OAuthToken, PublishMode


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "nested" / "app.db")
    await database.connect()
    yield database
    await database.close()


def draft(session_id: str = "s1", **overrides: object) -> Draft:
    values: dict[str, object] = {
        "session_id": session_id,
        "image_key": "a" * 32 + ".png",
        "original_filename": "dog.png",
        "content_type": "image/png",
        "width": 10,
        "height": 10,
        "size_bytes": 100,
        "labels": [Label(name="dog")],
        "keywords": ["dog"],
    }
    values.update(overrides)
    return Draft(**values)  # type: ignore[arg-type]


class TestDatabase:
    async def test_connect_is_idempotent_on_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "x.db"
        for _ in range(2):
            database = Database(path)
            await database.connect()
            await database.ping()
            await database.close()

    async def test_not_connected(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "y.db")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = database.conn
        await database.close()  # no-op


class TestDraftRepository:
    async def test_drafts_saved_with_label_scores_still_load(self, db: Database) -> None:
        """Drafts from the ResNet-50 era stored a confidence per label."""
        repo = DraftRepository(db)
        saved = draft("s1")
        await repo.add(saved)
        legacy = saved.model_dump(mode="json")
        legacy["labels"] = [{"name": "dog", "score": 0.9}]
        await db.conn.execute(
            "UPDATE drafts SET data = ? WHERE id = ?", (json.dumps(legacy), saved.id)
        )
        await db.conn.commit()
        loaded = await repo.get(saved.id, "s1")
        assert loaded is not None
        assert loaded.labels == [Label(name="dog")]

    async def test_crud_is_scoped_to_session(self, db: Database) -> None:
        repo = DraftRepository(db)
        mine = draft("s1")
        theirs = draft("s2")
        await repo.add(mine)
        await repo.add(theirs)

        assert await repo.get(mine.id, "s1") == mine
        assert await repo.get(mine.id, "s2") is None  # no IDOR
        assert await repo.count_for_session("s1") == 1
        assert [d.id for d in await repo.list_for_session("s1")] == [mine.id]

        updated = await repo.save(
            mine.model_copy(update={"caption": "Hi", "status": DraftStatus.GENERATED})
        )
        assert updated.updated_at >= mine.updated_at
        stored = await repo.get(mine.id, "s1")
        assert stored is not None
        assert stored.caption == "Hi"

        # Saving under the wrong session changes nothing.
        await repo.save(theirs.model_copy(update={"session_id": "s1", "caption": "hijack"}))
        other = await repo.get(theirs.id, "s2")
        assert other is not None
        assert other.caption == ""

        assert await repo.delete(mine.id, "s2") is None
        deleted = await repo.delete(mine.id, "s1")
        assert deleted is not None
        assert deleted.id == mine.id
        assert await repo.get(mine.id, "s1") is None

    async def test_list_order_and_limit(self, db: Database) -> None:
        repo = DraftRepository(db)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        drafts = [draft(created_at=base + timedelta(minutes=i)) for i in range(3)]
        for item in drafts:
            await repo.add(item)
        listed = await repo.list_for_session("s1", limit=2)
        assert [d.id for d in listed] == [drafts[2].id, drafts[1].id]

    async def test_publish_record_roundtrip(self, db: Database) -> None:
        repo = DraftRepository(db)
        item = draft()
        await repo.add(item)
        record = PublishRecord(
            provider="buffer",
            mode=PublishMode.SCHEDULE,
            profile_ids=["p"],
            scheduled_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await repo.save(item.model_copy(update={"publish": record}))
        stored = await repo.get(item.id, "s1")
        assert stored is not None
        assert stored.publish == record

    async def test_purge(self, db: Database) -> None:
        repo = DraftRepository(db)
        old = draft(image_key="b" * 32 + ".png")
        await repo.add(old)
        assert await repo.purge_older_than(datetime.now(UTC) - timedelta(hours=1)) == []
        assert await repo.purge_older_than(datetime.now(UTC) + timedelta(seconds=1)) == [
            old.image_key
        ]
        assert await repo.count_for_session("s1") == 0


class TestTokenRepository:
    async def test_encrypted_roundtrip_upsert_and_delete(self, db: Database) -> None:
        repo = TokenRepository(db, TokenCipher.from_secrets(session_secret="k" * 40))
        await repo.save("s1", "buffer", OAuthToken(access_token="first"))
        await repo.save("s1", "buffer", OAuthToken(access_token="second"))
        assert await repo.get("s1", "buffer") == OAuthToken(access_token="second")
        assert await repo.get("s2", "buffer") is None

        async with db.conn.execute("SELECT ciphertext FROM oauth_tokens") as cursor:
            rows = list(await cursor.fetchall())
        assert len(rows) == 1
        assert b"second" not in rows[0]["ciphertext"]

        await repo.delete("s1", "buffer")
        assert await repo.get("s1", "buffer") is None

    async def test_unreadable_token_is_discarded(self, db: Database) -> None:
        writer = TokenRepository(db, TokenCipher.from_secrets(session_secret="old" * 20))
        await writer.save("s1", "buffer", OAuthToken(access_token="t"))
        reader = TokenRepository(db, TokenCipher.from_secrets(session_secret="new" * 20))
        assert await reader.get("s1", "buffer") is None
        assert await writer.get("s1", "buffer") is None  # row was deleted

    async def test_purge(self, db: Database) -> None:
        repo = TokenRepository(db, TokenCipher.from_secrets(session_secret="k" * 40))
        await repo.save("s1", "buffer", OAuthToken(access_token="t"))
        assert await repo.purge_older_than(datetime.now(UTC) - timedelta(days=1)) == 0
        assert await repo.purge_older_than(datetime.now(UTC) + timedelta(seconds=1)) == 1
