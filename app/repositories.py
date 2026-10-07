"""Data access for drafts and OAuth tokens.

Every query is scoped by ``session_id``: a session can only ever read or
modify its own drafts and tokens (no IDOR even if a draft id leaks).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from app.db import Database
from app.models import Draft, utcnow
from app.security import TokenCipher, TokenDecryptionError
from services.publishing import OAuthToken

logger = logging.getLogger(__name__)


class DraftRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, draft: Draft) -> None:
        await self._db.conn.execute(
            "INSERT INTO drafts (id, session_id, image_key, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                draft.id,
                draft.session_id,
                draft.image_key,
                draft.model_dump_json(),
                draft.created_at.isoformat(),
                draft.updated_at.isoformat(),
            ),
        )
        await self._db.conn.commit()

    async def get(self, draft_id: str, session_id: str) -> Draft | None:
        async with self._db.conn.execute(
            "SELECT data FROM drafts WHERE id = ? AND session_id = ?", (draft_id, session_id)
        ) as cursor:
            row = await cursor.fetchone()
        return Draft.model_validate_json(row["data"]) if row else None

    async def list_for_session(self, session_id: str, *, limit: int = 100) -> list[Draft]:
        async with self._db.conn.execute(
            "SELECT data FROM drafts WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [Draft.model_validate_json(row["data"]) for row in rows]

    async def count_for_session(self, session_id: str) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    async def save(self, draft: Draft) -> Draft:
        """Persist changes to an existing draft (bumps ``updated_at``)."""
        updated = draft.model_copy(update={"updated_at": utcnow()})
        await self._db.conn.execute(
            "UPDATE drafts SET data = ?, updated_at = ? WHERE id = ? AND session_id = ?",
            (updated.model_dump_json(), updated.updated_at.isoformat(), draft.id, draft.session_id),
        )
        await self._db.conn.commit()
        return updated

    async def delete(self, draft_id: str, session_id: str) -> Draft | None:
        draft = await self.get(draft_id, session_id)
        if draft is None:
            return None
        await self._db.conn.execute(
            "DELETE FROM drafts WHERE id = ? AND session_id = ?", (draft_id, session_id)
        )
        await self._db.conn.commit()
        return draft

    async def purge_older_than(self, cutoff: datetime) -> list[str]:
        """Delete drafts untouched since ``cutoff``; return their image keys."""
        async with self._db.conn.execute(
            "SELECT image_key FROM drafts WHERE updated_at < ?", (cutoff.isoformat(),)
        ) as cursor:
            keys = [row["image_key"] for row in await cursor.fetchall()]
        if keys:
            await self._db.conn.execute(
                "DELETE FROM drafts WHERE updated_at < ?", (cutoff.isoformat(),)
            )
            await self._db.conn.commit()
        return keys


class TokenRepository:
    """Encrypted OAuth token storage keyed by ``(session_id, provider)``."""

    def __init__(self, db: Database, cipher: TokenCipher) -> None:
        self._db = db
        self._cipher = cipher

    async def save(self, session_id: str, provider: str, token: OAuthToken) -> None:
        now = utcnow().isoformat()
        ciphertext = self._cipher.encrypt(json.dumps(token.to_dict()))
        await self._db.conn.execute(
            "INSERT INTO oauth_tokens (session_id, provider, ciphertext, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id, provider) DO UPDATE SET "
            "ciphertext = excluded.ciphertext, updated_at = excluded.updated_at",
            (session_id, provider, ciphertext, now, now),
        )
        await self._db.conn.commit()

    async def get(self, session_id: str, provider: str) -> OAuthToken | None:
        async with self._db.conn.execute(
            "SELECT ciphertext FROM oauth_tokens WHERE session_id = ? AND provider = ?",
            (session_id, provider),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return OAuthToken.from_dict(json.loads(self._cipher.decrypt(row["ciphertext"])))
        except (TokenDecryptionError, ValueError, KeyError, TypeError):
            logger.warning("Discarding unreadable stored token", extra={"provider": provider})
            await self.delete(session_id, provider)
            return None

    async def delete(self, session_id: str, provider: str) -> None:
        await self._db.conn.execute(
            "DELETE FROM oauth_tokens WHERE session_id = ? AND provider = ?",
            (session_id, provider),
        )
        await self._db.conn.commit()

    async def purge_older_than(self, cutoff: datetime) -> int:
        cursor = await self._db.conn.execute(
            "DELETE FROM oauth_tokens WHERE updated_at < ?", (cutoff.isoformat(),)
        )
        await self._db.conn.commit()
        return cursor.rowcount
