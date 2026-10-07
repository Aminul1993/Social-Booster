"""Upload storage abstraction.

The application only talks to the :class:`Storage` protocol. The default
:class:`LocalFileStorage` writes to a directory that is served under
``/uploads``; an object-store implementation (S3, GCS, Azure Blob) only needs
to implement the same five methods and return public URLs from
:meth:`Storage.public_path`.

Storage keys are always generated server-side (``<32 hex chars>.<ext>``) and
validated on every access, which rules out path traversal.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiofiles
import aiofiles.os

from services.errors import StorageError
from services.images import SUPPORTED_EXTENSIONS

_KEY_RE = re.compile(r"^[0-9a-f]{32}\.(?:" + "|".join(sorted(SUPPORTED_EXTENSIONS)) + r")$")


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Metadata about a stored file."""

    key: str
    size: int
    content_type: str


@runtime_checkable
class Storage(Protocol):
    """Contract for upload storage back-ends."""

    async def save(self, data: bytes, *, extension: str, content_type: str) -> StoredObject:
        """Persist ``data`` under a new random key."""
        ...

    async def read(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""
        ...

    async def delete(self, key: str) -> bool:
        """Delete ``key``; return ``False`` when it did not exist."""
        ...

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` exists."""
        ...

    def public_path(self, key: str) -> str:
        """URL path (or absolute URL) under which ``key`` is publicly served."""
        ...

    async def check_health(self) -> None:
        """Raise :class:`StorageError` when the back-end is not writable."""
        ...


def is_valid_key(key: str) -> bool:
    """Whether ``key`` has the exact shape of a server-generated storage key."""
    return bool(_KEY_RE.fullmatch(key))


class LocalFileStorage:
    """Stores uploads on the local filesystem (a Docker volume in production)."""

    def __init__(self, root: Path, *, url_prefix: str = "/uploads") -> None:
        self.root = root.resolve()
        self.url_prefix = url_prefix.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not is_valid_key(key):
            raise StorageError(f"Invalid storage key: {key!r}")
        return self.root / key

    async def save(self, data: bytes, *, extension: str, content_type: str) -> StoredObject:
        extension = extension.lower().lstrip(".")
        if extension not in SUPPORTED_EXTENSIONS:
            raise StorageError(f"Unsupported file extension: {extension!r}")
        key = f"{uuid.uuid4().hex}.{extension}"
        path = self._path(key)
        temp_path = path.with_name(f".{key}.tmp")
        try:
            async with aiofiles.open(temp_path, "wb") as handle:
                await handle.write(data)
            # Atomic publish: readers never observe a half-written file.
            await aiofiles.os.replace(temp_path, path)
        except OSError as exc:
            await self._silent_remove(temp_path)
            raise StorageError("Could not save the uploaded file.") from exc
        return StoredObject(key=key, size=len(data), content_type=content_type)

    async def read(self, key: str) -> bytes:
        try:
            async with aiofiles.open(self._path(key), "rb") as handle:
                return await handle.read()
        except FileNotFoundError as exc:
            raise StorageError("The file no longer exists.") from exc

    async def delete(self, key: str) -> bool:
        try:
            await aiofiles.os.remove(self._path(key))
        except FileNotFoundError:
            return False
        return True

    async def exists(self, key: str) -> bool:
        if not is_valid_key(key):
            return False
        return await aiofiles.os.path.isfile(self._path(key))

    def public_path(self, key: str) -> str:
        self._path(key)  # validates the key
        return f"{self.url_prefix}/{key}"

    async def check_health(self) -> None:
        probe = self.root / f".healthcheck-{uuid.uuid4().hex}"
        try:
            async with aiofiles.open(probe, "wb") as handle:
                await handle.write(b"ok")
        except OSError as exc:
            raise StorageError(f"Upload directory is not writable: {exc}") from exc
        finally:
            await self._silent_remove(probe)

    @staticmethod
    async def _silent_remove(path: Path) -> None:
        try:
            await aiofiles.os.remove(path)
        except OSError:
            return
