from __future__ import annotations

from pathlib import Path

import pytest

from services.errors import StorageError
from services.storage import LocalFileStorage, Storage, is_valid_key


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "uploads", url_prefix="/uploads/")


async def test_implements_protocol(storage: LocalFileStorage) -> None:
    assert isinstance(storage, Storage)


async def test_save_read_exists_delete_roundtrip(storage: LocalFileStorage) -> None:
    stored = await storage.save(b"hello", extension=".PNG", content_type="image/png")
    assert is_valid_key(stored.key)
    assert stored.key.endswith(".png")
    assert stored.size == 5
    assert await storage.exists(stored.key)
    assert await storage.read(stored.key) == b"hello"
    assert storage.public_path(stored.key) == f"/uploads/{stored.key}"
    assert not list(storage.root.glob(".*.tmp"))

    assert await storage.delete(stored.key) is True
    assert await storage.delete(stored.key) is False
    assert not await storage.exists(stored.key)


async def test_read_missing_raises(storage: LocalFileStorage) -> None:
    with pytest.raises(StorageError, match="no longer exists"):
        await storage.read("0" * 32 + ".png")


@pytest.mark.parametrize(
    "key", ["../secret.png", "abc.png", "0" * 32 + ".exe", "0" * 32 + ".png/../x", ""]
)
async def test_rejects_invalid_keys(storage: LocalFileStorage, key: str) -> None:
    assert not is_valid_key(key)
    assert await storage.exists(key) is False
    with pytest.raises(StorageError, match="Invalid storage key"):
        storage.public_path(key)
    with pytest.raises(StorageError):
        await storage.read(key)


async def test_rejects_unsupported_extension(storage: LocalFileStorage) -> None:
    with pytest.raises(StorageError, match="Unsupported"):
        await storage.save(b"x", extension="gif", content_type="image/gif")


async def test_save_failure_is_wrapped(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_replace(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("services.storage.aiofiles.os.replace", broken_replace)
    with pytest.raises(StorageError, match="Could not save"):
        await storage.save(b"data", extension="png", content_type="image/png")
    assert not list(storage.root.iterdir())


async def test_health_check(storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    await storage.check_health()
    assert not list(storage.root.iterdir())

    def broken_open(*_: object, **__: object) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr("services.storage.aiofiles.open", broken_open)
    with pytest.raises(StorageError, match="not writable"):
        await storage.check_health()
