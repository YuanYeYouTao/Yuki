"""Transactional expiring artifact index with immutable, non-followed blobs."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4


class WorkspaceError(RuntimeError):
    pass


class WorkspaceStore:
    def __init__(
        self,
        root: Path,
        *,
        ttl: int = 86400,
        capacity: int = 512 * 1024 * 1024,
        max_file: int = 200 * 1024 * 1024,
        max_objects: int = 1000,
    ) -> None:
        self.root = root.absolute()
        self.ttl, self.capacity, self.max_file, self.max_objects = (
            ttl,
            capacity,
            max_file,
            max_objects,
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise WorkspaceError("unsafe_workspace_root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        index = self.root / "manifest.sqlite3"
        if index.is_symlink() or (index.exists() and index.stat().st_nlink != 1):
            raise WorkspaceError("unsafe_workspace_index")
        with sqlite3.connect(index, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute(
                "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "blob TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, "
                "revision INTEGER NOT NULL, created_at REAL NOT NULL, "
                "modified_at REAL NOT NULL, expires_at REAL NOT NULL)"
            )
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _id(value: str) -> str:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError):
            raise WorkspaceError("invalid_artifact_id") from None
        if str(parsed) != value:
            raise WorkspaceError("invalid_artifact_id")
        return value

    def _blob(self, name: str) -> Path:
        if not name.endswith(".blob"):
            raise WorkspaceError("invalid_blob")
        self._id(name[:-5])
        return self.root / name

    def _row(self, db: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM artifacts WHERE id=?", (self._id(artifact_id),)).fetchone()
        if row is None:
            raise WorkspaceError("artifact_not_found")
        if row["expires_at"] <= time.time():
            raise WorkspaceError("artifact_expired")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["id"],
            "name": row["name"],
            "size": row["size"],
            "sha256": row["sha256"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "modified_at": row["modified_at"],
            "expires_at": row["expires_at"],
        }

    def _cleanup(self, db: sqlite3.Connection) -> int:
        count = db.execute("DELETE FROM artifacts WHERE expires_at<=?", (time.time(),)).rowcount
        referenced = {row[0] for row in db.execute("SELECT blob FROM artifacts")}
        for path in self.root.glob("*.blob"):
            if path.name not in referenced:
                self._blob(path.name).unlink(missing_ok=True)
        for row in db.execute("SELECT id,blob FROM artifacts").fetchall():
            path = self._blob(row["blob"])
            if not path.exists() or path.is_symlink() or path.stat().st_nlink != 1:
                db.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
        return count

    def cleanup(self) -> int:
        with self._transaction() as db:
            return self._cleanup(db)

    def write(
        self,
        name: str,
        data: bytes,
        *,
        artifact_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if (
            not name
            or len(name) > 128
            or Path(name).name != name
            or any(c in name for c in "\\/:\x00")
        ):
            raise WorkspaceError("invalid_artifact_name")
        if len(data) > self.max_file:
            raise WorkspaceError("artifact_too_large")
        digest = hashlib.sha256(data).hexdigest()
        with self._transaction() as db:
            self._cleanup(db)
            old = self._row(db, artifact_id) if artifact_id else None
            if old is not None and expected_revision != old["revision"]:
                raise WorkspaceError("version_conflict")
            if old is None and expected_revision is not None:
                raise WorkspaceError("invalid_revision")
            total, count = db.execute(
                "SELECT coalesce(sum(size),0),count(*) FROM artifacts"
            ).fetchone()
            if total - (old["size"] if old else 0) + len(data) > self.capacity or (
                old is None and count >= self.max_objects
            ):
                raise WorkspaceError("workspace_full")
            if old is not None and old["sha256"] == digest:
                # Renaming does not refresh TTL; still advances CAS revision.
                if old["name"] != name:
                    db.execute(
                        "UPDATE artifacts SET name=?,revision=revision+1 WHERE id=?",
                        (name, artifact_id),
                    )
                return self._metadata(self._row(db, artifact_id or ""))
            blob = f"{uuid4()}.blob"
            path = self._blob(blob)
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            now = time.time()
            identity = artifact_id or str(uuid4())
            db.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    name,
                    blob,
                    digest,
                    len(data),
                    old["revision"] + 1 if old else 1,
                    old["created_at"] if old else now,
                    now,
                    now + self.ttl,
                ),
            )
            # Obsolete blobs are reclaimed after commit by the next cleanup.
            return self._metadata(self._row(db, identity))

    def publish_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        """Publish a whole successful sandbox result, or no visible artifacts."""
        with self._transaction() as db:
            self._cleanup(db)
            total, count = db.execute(
                "SELECT coalesce(sum(size),0),count(*) FROM artifacts"
            ).fetchone()
            if (
                count + len(files) > self.max_objects
                or total + sum(len(data) for _, data in files) > self.capacity
            ):
                raise WorkspaceError("workspace_full")
            identities = []
            for name, data in files:
                if (
                    not name
                    or len(name) > 128
                    or Path(name).name != name
                    or any(c in name for c in "\\/:\x00")
                ):
                    raise WorkspaceError("invalid_artifact_name")
                if len(data) > self.max_file:
                    raise WorkspaceError("artifact_too_large")
                identity, blob = str(uuid4()), f"{uuid4()}.blob"
                with self._blob(blob).open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                now = time.time()
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        name,
                        blob,
                        hashlib.sha256(data).hexdigest(),
                        len(data),
                        1,
                        now,
                        now,
                        now + self.ttl,
                    ),
                )
                identities.append(identity)
            return [self._metadata(self._row(db, identity)) for identity in identities]

    def read_bytes(self, artifact_id: str) -> tuple[dict[str, Any], bytes]:
        with self._transaction() as db:
            row = self._row(db, artifact_id)
            path = self._blob(row["blob"])
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > self.max_file
                ):
                    raise WorkspaceError("unsafe_artifact")
                data = stream.read(self.max_file + 1)
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise WorkspaceError("artifact_corrupt")
            return self._metadata(row), data

    def read(self, artifact_id: str) -> dict[str, Any]:
        metadata, data = self.read_bytes(artifact_id)
        try:
            import codecs

            value = codecs.getincrementaldecoder("utf-8")().decode(
                data[:32768], final=len(data) <= 32768
            )
        except UnicodeDecodeError:
            return {**metadata, "binary": True}
        if "\x00" in value:
            return {**metadata, "binary": True}
        return {
            **metadata,
            "text": value,
            "truncated": len(data) > 32768,
            "external_untrusted": True,
        }

    def list(self, *, cursor: str = "", limit: int = 20) -> dict[str, Any]:
        if cursor:
            self._id(cursor)
        if not 1 <= limit <= 100:
            raise WorkspaceError("invalid_limit")
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM artifacts WHERE expires_at>? AND id>? ORDER BY id LIMIT ?",
                (time.time(), cursor, limit + 1),
            ).fetchall()
            return {
                "items": [self._metadata(row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    def delete(self, artifact_id: str, expected_revision: int) -> dict[str, Any]:
        with self._transaction() as db:
            row = self._row(db, artifact_id)
            if row["revision"] != expected_revision:
                raise WorkspaceError("version_conflict")
            db.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        self.cleanup()
        return {"deleted": True, "artifact_id": artifact_id}
