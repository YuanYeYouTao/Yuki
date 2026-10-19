"""File-owned sticker metadata and a disposable inverted cache.

This script runs inside the persistent environment, with the same filesystem
namespace as its terminal. It intentionally has no Bot/database dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

try:
    from .media import inspect_path
except ImportError:  # Installed next to this standalone script.
    from media import inspect_path  # type: ignore[no-redef,import-not-found]

CACHE_VERSION = 2


def identity(platform: str, digest: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"yuki:sticker:{platform}:{digest}"))


def terms(text: str) -> set[str]:
    values = set()
    for word in re.split(r"([\u3400-\u9fff]+)", text.casefold()):
        if word and "\u3400" <= word[0] <= "\u9fff":
            values.add(word)
            values.update(word)
            values.update(word[index : index + 2] for index in range(len(word) - 1))
        else:
            values.update(re.findall(r"\w+|[^\w\s]", word))
    return values


def record_terms(record: dict[str, Any]) -> set[str]:
    return terms(
        " ".join(
            (*record.get("tags", ()), record.get("description", ""), record.get("ocr_text", ""))
        )
    )


class StickerIndex:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = workspace / "stickers"

    def directory(self, platform: str) -> Path:
        if not platform or Path(platform).name != platform or platform in {".", ".."}:
            raise ValueError("invalid_platform_directory")
        return self.root / platform

    def metadata_path(self, platform: str, asset_id: str) -> Path:
        return self.directory(platform) / "index" / (str(UUID(asset_id)) + ".json")

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid_sticker_json")
        if not isinstance(value.get("tags", []), list) or not all(
            isinstance(tag, str) for tag in value.get("tags", [])
        ):
            raise ValueError("invalid_sticker_tags")
        if not all(isinstance(value.get(key, ""), str) for key in ("description", "ocr_text")):
            raise ValueError("invalid_sticker_text")
        return value

    @staticmethod
    def write_json(path: Path, value: dict[str, Any], *, create: bool = False) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / (".sticker-" + uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if create:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return False
            else:
                os.replace(temporary, path)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def facts(self, path: Path, *, inspect_pixels: bool = True) -> dict[str, Any]:
        with path.open("rb") as stream:
            result = inspect_path(
                stream.fileno(),
                path.relative_to(self.workspace).as_posix(),
                inspect_pixels=inspect_pixels,
            )
        if result is None:
            raise ValueError("file_changed_during_read")
        return result

    def normalize(
        self, platform: str, facts: dict[str, Any], old: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "id": identity(platform, facts["sha256"]),
            "platform": platform,
            **{
                key: old[key]
                for key in ("width", "height", "frame_count", "animated")
                if key in old and old.get("sha256") == facts["sha256"]
            },
            **facts,
            "is_sticker": bool(old.get("is_sticker", False)),
            "tags": old.get("tags", []),
            "description": old.get("description", ""),
            "ocr_text": old.get("ocr_text", ""),
        }

    def original_path(self, platform: str, relative: str) -> Path:
        original = self.workspace / relative.removeprefix("/workspace/")
        original.resolve().relative_to((self.directory(platform) / "files").resolve())
        return original

    def lookup(self, platform: str, digest: str) -> dict[str, Any]:
        try:
            old = self.read_json(self.metadata_path(platform, identity(platform, digest)))
            facts = self.facts(
                self.original_path(platform, old["relative_path"]), inspect_pixels=False
            )
            if facts["sha256"] == digest:
                return {"asset": self.normalize(platform, facts, old)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {"asset": None}

    @contextmanager
    def cache_writer(self, platform: str) -> Iterator[None]:
        import fcntl

        directory = self.directory(platform)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".search.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def corrupt(error: sqlite3.DatabaseError) -> bool:
        return (getattr(error, "sqlite_errorcode", 0) & 255) in {
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB,
        }

    @staticmethod
    def valid_schema(db: sqlite3.Connection) -> bool:
        return {"id"} <= {row[1] for row in db.execute("PRAGMA table_info(assets)")} and {
            "term",
            "asset_id",
        } <= {row[1] for row in db.execute("PRAGMA table_info(terms)")}

    def _unlink_cache(self, platform: str) -> None:
        path = self.directory(platform) / "search.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    def cache(self, platform: str) -> sqlite3.Connection:
        path = self.directory(platform) / "search.sqlite3"
        db = None
        try:
            db = sqlite3.connect(path)
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables and (
                not self.valid_schema(db)
                or db.execute("PRAGMA user_version").fetchone()[0] not in {0, CACHE_VERSION}
            ):
                db.close()
                self._unlink_cache(platform)
                db = sqlite3.connect(path)
        except sqlite3.DatabaseError as error:
            if db is not None:
                db.close()
            if not self.corrupt(error):
                raise
            self._unlink_cache(platform)
            db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            "CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY);"
            "CREATE TABLE IF NOT EXISTS terms(term TEXT NOT NULL,asset_id TEXT NOT NULL,"
            "PRIMARY KEY(term,asset_id));"
            "CREATE INDEX IF NOT EXISTS terms_asset ON terms(asset_id);"
        )
        return db

    def ready(self, platform: str) -> bool:
        path = self.directory(platform) / "search.sqlite3"
        if not path.exists():
            return False
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
                valid = self.valid_schema(db)
                if valid:
                    db.execute("SELECT id FROM assets LIMIT 1").fetchone()
                    db.execute("SELECT term,asset_id FROM terms LIMIT 1").fetchone()
                    return bool(db.execute("PRAGMA user_version").fetchone()[0] == CACHE_VERSION)
        except sqlite3.DatabaseError as error:
            if not path.exists():
                return False
            if not self.corrupt(error):
                raise
        self.discard_cache(platform)
        return False

    def update_cache(self, platform: str, asset_id: str) -> None:
        self._cache_batch(platform, (asset_id,))

    def _cache_batch(
        self,
        platform: str,
        asset_ids: tuple[str, ...],
        *,
        invalid: tuple[str, dict[str, Any] | None] | None = None,
    ) -> None:
        # Fresh metadata is read under cache coordination, before BEGIN. Hashes
        # and optional pixels are prepared outside this short cache-only lock.
        with self.cache_writer(platform):
            prepared = []
            current: dict[str, Any] | None
            for asset_id in asset_ids:
                try:
                    current = self.read_json(self.metadata_path(platform, asset_id))
                    original = self.original_path(platform, current["relative_path"])
                    if not original.is_file() or (
                        invalid is not None and invalid[0] == asset_id and current == invalid[1]
                    ):
                        current = None
                    indexed = record_terms(current) if current is not None else set()
                except (OSError, ValueError, TypeError, KeyError):
                    current, indexed = None, set()
                prepared.append((asset_id, current, indexed))
            db = self.cache(platform)
            try:
                with db:
                    for asset_id, current, indexed in prepared:
                        db.execute("DELETE FROM terms WHERE asset_id=?", (asset_id,))
                        if current is None:
                            db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
                        else:
                            db.execute("INSERT OR IGNORE INTO assets VALUES(?)", (asset_id,))
                            db.executemany(
                                "INSERT OR IGNORE INTO terms VALUES(?,?)",
                                ((term, asset_id) for term in indexed),
                            )
            finally:
                db.close()

    def catalog(
        self,
        platform: str,
        path: str,
        *,
        tags: list[str] | None = None,
        is_sticker: bool = False,
        _facts: dict[str, Any] | None = None,
        _index: bool = True,
    ) -> dict[str, Any]:
        original = self.workspace / path.removeprefix("/workspace/")
        facts = self.facts(original) if _facts is None else _facts
        asset_id = identity(platform, facts["sha256"])
        target = self.metadata_path(platform, asset_id)
        record = self.normalize(platform, facts, {"tags": tags or [], "is_sticker": is_sticker})
        created = self.write_json(target, record, create=True)
        if not created:
            old = self.read_json(target)
            try:
                old_path = self.original_path(platform, old.get("relative_path", ""))
                existing = facts if old_path == original else self.facts(old_path)
            except (OSError, ValueError):
                existing = None
            # Keep a living canonical file, including a terminal rename adopted by rebuild.
            chosen = existing if existing and existing["sha256"] == facts["sha256"] else facts
            # Keep completed edits, including a rename adopted while hashing.
            # Those old facts no longer describe the current canonical path.
            current = self.read_json(target)
            record = (
                self.normalize(platform, chosen, current)
                if current.get("relative_path") == old.get("relative_path")
                else dict(current)
            )
            if is_sticker:
                record["is_sticker"] = True
            if record != current:
                self.write_json(target, record)
        if _index:
            self.update_cache(platform, asset_id)
        return {"asset": record, "created": created}

    def update(self, path: str, *, tags: list[str] | None = None) -> dict[str, Any]:
        selected = self.workspace / path.removeprefix("/workspace/")
        relative = selected.relative_to(self.root)
        platform = relative.parts[0]
        if relative.parts[1] == "files":
            result = self.catalog(platform, path, tags=tags, _index=False)
            record = result["asset"]
            if tags is not None and record["tags"] != tags:
                record["tags"] = tags
                self.write_json(self.metadata_path(platform, record["id"]), record)
            self.update_cache(platform, record["id"])
            return {"asset": record}
        old = self.read_json(selected)
        if tags is not None:
            old["tags"] = tags
        try:
            original = self.original_path(platform, old["relative_path"])
        except ValueError:
            # An explicitly copied JSON carries its text to the same target files layout.
            parts = Path(old["relative_path"]).parts
            original = self.directory(platform) / "files" / Path(*parts[3:])
        facts = self.facts(original)
        record = self.normalize(platform, facts, old)
        canonical = self.metadata_path(platform, record["id"])
        self.write_json(canonical, record)
        if selected != canonical:
            selected.unlink(missing_ok=True)
        affected = {record["id"], selected.stem}
        if isinstance(old.get("id"), str):
            affected.add(old["id"])
        self._cache_batch(platform, tuple(affected))
        return {"asset": record}

    def annotate(
        self, asset: dict[str, Any], *, tags: list[str], description: str, ocr_text: str
    ) -> dict[str, Any]:
        path = self.metadata_path(asset["platform"], asset["id"])
        current = self.read_json(path)
        # Ordinary last-writer semantics; no editing lease, revision or permission split.
        current.update(tags=tags, description=description, ocr_text=ocr_text)
        self.write_json(path, current)
        self.update_cache(asset["platform"], asset["id"])
        return {"asset": current}

    def rebuild(self, platform: str | None = None) -> dict[str, Any]:
        platforms = (
            [platform]
            if platform is not None
            else (
                [path.name for path in self.root.iterdir() if path.is_dir()]
                if self.root.exists()
                else []
            )
        )
        files = assets = 0
        errors = []
        for current in platforms:
            directory = self.directory(current)
            initial = set()
            if self.ready(current):
                with closing(sqlite3.connect(directory / "search.sqlite3")) as db:
                    initial = {row[0] for row in db.execute("SELECT id FROM assets")}
            indexed = set()
            pending = []
            # Read each metadata file once to pair explicit cross-platform copies.
            copied: dict[str, dict[str, Any]] = {}
            observed: dict[Path, dict[str, Any]] = {}
            bad = set()
            if (directory / "index").exists():
                for metadata in (directory / "index").glob("*.json"):
                    try:
                        value = self.read_json(metadata)
                        copied.setdefault(value.get("sha256", ""), value)
                        observed[metadata] = value
                    except (OSError, ValueError, TypeError) as exc:
                        bad.add(metadata.stem)
                        errors.append({"path": str(metadata), "error": type(exc).__name__})
            originals = directory / "files"
            files_complete = True
            for path in originals.rglob("*") if originals.exists() else ():
                if not path.is_file() or path.is_symlink():
                    continue
                files += 1
                try:
                    facts = self.facts(path)
                    asset_id = identity(current, facts["sha256"])
                    if asset_id in bad:
                        continue
                    canonical = self.metadata_path(current, asset_id)
                    if not canonical.exists():
                        value = self.normalize(current, facts, copied.get(facts["sha256"], {}))
                        self.write_json(canonical, value, create=True)
                    result = self.catalog(
                        current, facts["relative_path"], _facts=facts, _index=False
                    )
                    indexed.add(result["asset"]["id"])
                    pending.append(result["asset"]["id"])
                    if len(pending) == 100:
                        self._cache_batch(current, tuple(pending))
                        pending.clear()
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    files_complete = False
                    errors.append({"path": str(path), "error": type(exc).__name__})
            # Renames and duplicate originals have already adopted their metadata.
            # An unreadable original is unknown, so keep its possible old tags.
            for metadata, value in observed.items() if files_complete else ():
                try:
                    previous_id = identity(current, value["sha256"])
                    if previous_id in indexed:
                        continue
                    original = self.original_path(current, value["relative_path"])
                    remaining: dict[str, Any] | None
                    try:
                        remaining = self.facts(original, inspect_pixels=False)
                    except FileNotFoundError:
                        remaining = None
                    if remaining is not None and remaining["sha256"] == value["sha256"]:
                        continue
                    with self.cache_writer(current):
                        # Do not unlink a completed terminal edit seen after discovery.
                        if self.read_json(metadata) == value:
                            metadata.unlink()
                            pending.append(previous_id)
                except FileNotFoundError:
                    pending.append(previous_id)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append({"path": str(metadata), "error": type(exc).__name__})
            pending.extend(initial - indexed)
            for start in range(0, len(pending), 100):
                self._cache_batch(current, tuple(pending[start : start + 100]))
            with self.cache_writer(current):
                db = self.cache(current)
                db.execute(f"PRAGMA user_version={CACHE_VERSION}")
                db.close()
            assets += len(indexed)
        for error in errors:
            print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return {"files": files, "assets": assets, "errors": len(errors)}

    def discard_cache(self, platform: str) -> None:
        with self.cache_writer(platform):
            self._unlink_cache(platform)

    def search(self, platform: str, query: str, *, limit: int = 20) -> dict[str, Any]:
        if not self.ready(platform):
            return {"needs_build": True}
        try:
            return self._search_ready(platform, query, limit=limit)
        except sqlite3.DatabaseError as error:
            # Corrupted pages are rebuildable; SQL, lock and storage failures stay failures.
            if not self.corrupt(error):
                if (
                    getattr(error, "sqlite_errorcode", 0) & 255
                ) == sqlite3.SQLITE_ERROR and not self.ready(platform):
                    # A terminal can remove the cache between readiness and query.
                    return {"needs_build": True}
                raise
            self.discard_cache(platform)
            return {"needs_build": True}

    def _search_ready(self, platform: str, query: str, *, limit: int) -> dict[str, Any]:
        wanted = terms(query)
        if not wanted:
            return {"items": []}
        sql = (
            "SELECT asset_id FROM terms WHERE term IN (SELECT value FROM json_each(?)) "
            "GROUP BY asset_id ORDER BY COUNT(*) DESC,asset_id LIMIT ?"
        )
        while True:
            with closing(sqlite3.connect(self.directory(platform) / "search.sqlite3")) as db:
                rows = db.execute(sql, (json.dumps(sorted(wanted)), limit)).fetchall()
            if not rows:
                return {"items": []}
            results = []
            for (asset_id,) in rows:
                value = None
                try:
                    value = self.read_json(self.metadata_path(platform, asset_id))
                    facts = self.facts(
                        self.original_path(platform, value["relative_path"]), inspect_pixels=False
                    )
                    actual = self.normalize(platform, facts, value)
                    valid = actual["id"] == asset_id and bool(record_terms(value) & wanted)
                except (OSError, ValueError, TypeError, KeyError):
                    valid = False
                if valid:
                    results.append(actual)
                else:
                    self._cache_batch(platform, (asset_id,), invalid=(asset_id, value))
            if results:
                return {"items": results}

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request["command"]
        arguments = {key: value for key, value in request.items() if key != "command"}
        result = cast(dict[str, Any], getattr(self, command)(**arguments))
        # Descriptive text stays in the canonical file; the socket carries execution facts.
        for record in [result.get("asset"), *result.get("items", [])]:
            if record is not None:
                record.pop("description", None)
                record.pop("ocr_text", None)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument(
        "--request", action="store_true", help="Read private request JSON from stdin"
    )
    sub = parser.add_subparsers(dest="command")
    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--platform")
    update = sub.add_parser("update")
    update.add_argument("path")
    update.add_argument(
        "--tag", dest="tags", action="append", help="Replace short tags; repeat for each tag"
    )
    search = sub.add_parser("search")
    search.add_argument("--platform", required=True)
    search.add_argument("query")
    args = vars(parser.parse_args())
    workspace, request = args.pop("workspace"), args.pop("request")
    engine = StickerIndex(workspace)
    result = engine.execute(json.load(sys.stdin) if request else args)
    if not request and args["command"] == "search":
        while result.get("needs_build"):
            engine.rebuild(args["platform"])
            result = engine.execute(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
