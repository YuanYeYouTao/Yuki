"""Only a genuinely missing disposable cache is rebuilt after its open race."""

import sqlite3

import pytest

from yuki.emoji.sticker_index import StickerIndex


@pytest.mark.parametrize("method", ["search", "ready"])
def test_cache_disappearing_between_exists_and_open_is_rebuildable(tmp_path, monkeypatch, method):
    index = StickerIndex(tmp_path)
    cache = index.directory("qq") / "search.sqlite3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.touch()
    connect = sqlite3.connect

    def missing(*args, **kwargs):
        cache.unlink(missing_ok=True)
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", missing)
    assert (index.search("qq", "happy") if method == "search" else index.ready("qq")) == (
        {"needs_build": True} if method == "search" else False
    )


@pytest.mark.parametrize(
    "code",
    [
        sqlite3.SQLITE_ERROR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_PERM,
    ],
)
@pytest.mark.parametrize("method", ["search", "ready"])
def test_other_cache_errors_are_not_disguised_as_a_missing_cache(
    tmp_path, monkeypatch, code, method
):
    index = StickerIndex(tmp_path)
    cache = index.directory("qq") / "search.sqlite3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.touch()
    error = sqlite3.OperationalError("genuine SQLite failure")
    error.sqlite_errorcode = code

    def fail(*args, **kwargs):
        cache.unlink(missing_ok=True)
        raise error

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        index.search("qq", "happy") if method == "search" else index.ready("qq")
    assert caught.value is error
