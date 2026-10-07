"""SQLite persistence (async via aiosqlite).

SQLite in WAL mode handles the write volume of this app comfortably and is
safe for several worker processes on one host. Drafts are stored as JSON
documents next to the few indexed columns that queries filter on, so adding a
field to :class:`app.models.Draft` needs no migration.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    image_key   TEXT NOT NULL,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_drafts_session_created ON drafts (session_id, created_at);
CREATE INDEX IF NOT EXISTS ix_drafts_updated ON drafts (updated_at);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    session_id  TEXT NOT NULL,
    provider    TEXT NOT NULL,
    ciphertext  BLOB NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, provider)
);
CREATE INDEX IF NOT EXISTS ix_oauth_tokens_updated ON oauth_tokens (updated_at);
"""


class Database:
    """Owns the process-wide SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(_SCHEMA)
        async with conn.execute("SELECT version FROM schema_version") as cursor:
            row = await cursor.fetchone()
        if row is None:
            await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        await conn.commit()
        self._conn = conn
        logger.info("Database ready", extra={"path": str(self.path)})

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def ping(self) -> None:
        async with self.conn.execute("SELECT 1") as cursor:
            await cursor.fetchone()
