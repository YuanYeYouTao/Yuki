"""Transactional artifact snapshots; persistent by default, with bounded storage."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedArtifact:
    """Private, unpublished file; preparation does not hold a database transaction."""

    name: str
    pending: Path
    blob: str
    sha256: str
    size: int


class WorkspaceStore:
    def __init__(
        self,
        root: Path,
        *,
        ttl: int = 0,
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
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise WorkspaceError("unsafe_workspace_root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        index = self.root / "manifest.sqlite3"
        if index.is_symlink() or (index.exists() and index.stat().st_nlink != 1):
            raise WorkspaceError("unsafe_workspace_index")
        with closing(sqlite3.connect(index, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"artifacts", "artifact_snapshots"} <= tables:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, "
                    "name TEXT NOT NULL, "
                    "blob TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, "
                    "revision INTEGER NOT NULL, created_at REAL NOT NULL, "
                    "modified_at REAL NOT NULL, expires_at REAL NOT NULL)"
                )
                db.execute("CREATE TABLE IF NOT EXISTS artifact_snapshots (id TEXT PRIMARY KEY)")
                db.commit()
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
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
        row = db.execute(
            "SELECT *, EXISTS(SELECT 1 FROM artifact_snapshots s WHERE s.id=artifacts.id) "
            "AS immutable FROM artifacts WHERE id=?",
            (self._id(artifact_id),),
        ).fetchone()
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
            "expires_at": None if row["expires_at"] >= 253402300799 else row["expires_at"],
            "immutable": bool(row["immutable"]),
        }

    def _prune_metadata(self, db: sqlite3.Connection) -> int:
        if self.ttl == 0:
            # Existing, still-present files become persistent on first activation.
            db.execute("UPDATE artifacts SET expires_at=253402300799 WHERE expires_at<253402300799")
        count = db.execute("DELETE FROM artifacts WHERE expires_at<=?", (time.time(),)).rowcount
        db.execute("DELETE FROM artifact_snapshots WHERE id NOT IN (SELECT id FROM artifacts)")
        return count

    @staticmethod
    def _unlink(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # An open reader on Windows or a transient I/O failure can defer GC.
                pass

    def cleanup(self) -> int:
        # Discover files before the writer fence. A publisher can only introduce a
        # fresh UUID blob while holding that fence; it cannot resurrect a candidate.
        with self._transaction(write=False) as db:
            rows = db.execute("SELECT id,blob,revision FROM artifacts").fetchall()
        invalid = []
        for row in rows:
            path = self._blob(row["blob"])
            try:
                info = path.lstat()
                safe = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            except FileNotFoundError:
                safe = False
            if not safe:
                invalid.append((row["id"], row["blob"], row["revision"]))
        candidates = list(islice(self.root.glob("*.blob"), self.max_objects + 128))
        with self._transaction() as db:
            count = self._prune_metadata(db)
            db.executemany("DELETE FROM artifacts WHERE id=? AND blob=? AND revision=?", invalid)
            db.execute("DELETE FROM artifact_snapshots WHERE id NOT IN (SELECT id FROM artifacts)")
            referenced = {row[0] for row in db.execute("SELECT blob FROM artifacts")}
            garbage = [self._blob(path.name) for path in candidates if path.name not in referenced][
                :128
            ]
        self._unlink(garbage)
        return count

    @staticmethod
    def _validate_name(name: str) -> None:
        if (
            not name
            or len(name) > 128
            or Path(name).name != name
            or any(c in name for c in "\\/:\x00")
        ):
            raise WorkspaceError("invalid_artifact_name")

    def prepare_batch(self, files: list[tuple[str, bytes]]) -> list[PreparedArtifact]:
        """Run in a worker thread; only private pending files are visible here."""
        for name, data in files:
            self._validate_name(name)
            if len(data) > self.max_file:
                raise WorkspaceError("artifact_too_large")
        self.cleanup()
        return self._prepare_files(files)

    def _prepare_files(
        self, files: list[tuple[str, bytes]], *, digests: list[str] | None = None
    ) -> list[PreparedArtifact]:
        prepared = []
        try:
            for index, (name, data) in enumerate(files):
                blob = f"{uuid4()}.blob"
                pending = self.root / f"{uuid4()}.pending"
                try:
                    digest = digests[index] if digests else hashlib.sha256(data).hexdigest()
                    with pending.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    self._unlink([pending])
                    raise
                prepared.append(PreparedArtifact(name, pending, blob, digest, len(data)))
            return prepared
        except BaseException:
            self.discard_prepared(prepared)
            raise

    def discard_prepared(self, prepared: list[PreparedArtifact]) -> None:
        self._unlink([item.pending for item in prepared])

    def _publish_file(self, item: PreparedArtifact) -> None:
        if item.pending.parent != self.root:
            raise WorkspaceError("unsafe_artifact")
        info = item.pending.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != item.size:
            raise WorkspaceError("unsafe_artifact")
        os.replace(item.pending, self._blob(item.blob))

    def write(
        self,
        name: str,
        data: bytes,
        *,
        artifact_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._validate_name(name)
        if len(data) > self.max_file:
            raise WorkspaceError("artifact_too_large")
        self.cleanup()
        digest = hashlib.sha256(data).hexdigest()
        if artifact_id:
            with self._transaction(write=False) as db:
                observed = self._row(db, artifact_id)
                unchanged = (
                    observed["sha256"] == digest and observed["revision"] == expected_revision
                )
            if unchanged:
                with self._transaction() as db:
                    current = self._row(db, artifact_id)
                    if current["immutable"]:
                        raise WorkspaceError("artifact_is_immutable")
                    if current["revision"] != expected_revision:
                        raise WorkspaceError("version_conflict")
                    # An unchanged revision also fences the read's content digest.
                    if current["name"] != name:
                        db.execute(
                            "UPDATE artifacts SET name=?,revision=revision+1 WHERE id=?",
                            (name, artifact_id),
                        )
                    return self._metadata(self._row(db, artifact_id))
        prepared = self._prepare_files([(name, data)], digests=[digest])
        item = prepared[0]
        published = False
        retired: list[Path] = []
        try:
            with self._transaction() as db:
                self._prune_metadata(db)
                old = self._row(db, artifact_id) if artifact_id else None
                if old is not None and old["immutable"]:
                    raise WorkspaceError("artifact_is_immutable")
                if old is not None and expected_revision != old["revision"]:
                    raise WorkspaceError("version_conflict")
                if old is None and expected_revision is not None:
                    raise WorkspaceError("invalid_revision")
                total, count = db.execute(
                    "SELECT coalesce(sum(size),0),count(*) FROM artifacts"
                ).fetchone()
                if total - (old["size"] if old else 0) + item.size > self.capacity or (
                    old is None and count >= self.max_objects
                ):
                    raise WorkspaceError("workspace_full")
                if old is not None and old["sha256"] == item.sha256:
                    if old["name"] != name:
                        db.execute(
                            "UPDATE artifacts SET name=?,revision=revision+1 WHERE id=?",
                            (name, artifact_id),
                        )
                    result = self._metadata(self._row(db, artifact_id or ""))
                else:
                    self._publish_file(item)
                    now = time.time()
                    identity = artifact_id or str(uuid4())
                    db.execute(
                        "INSERT OR REPLACE INTO artifacts "
                        "(id,name,blob,sha256,size,revision,created_at,modified_at,expires_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            identity,
                            name,
                            item.blob,
                            item.sha256,
                            item.size,
                            old["revision"] + 1 if old else 1,
                            old["created_at"] if old else now,
                            now,
                            now + self.ttl if self.ttl else 253402300799,
                        ),
                    )
                    if old:
                        retired.append(self._blob(old["blob"]))
                    result = self._metadata(self._row(db, identity))
            published = True
            self._unlink(retired)
            return result
        finally:
            self.discard_prepared(prepared)
            if not published:
                self._unlink([self._blob(item.blob)])

    def publish_prepared_batch(self, prepared: list[PreparedArtifact]) -> list[dict[str, Any]]:
        """Short synchronous finish; the whole batch becomes visible in one commit.

        The preparer's thread owns pending-file disposal and subsequent bounded GC.
        """
        with self._transaction() as db:
            self._prune_metadata(db)
            total, count = db.execute(
                "SELECT coalesce(sum(size),0),count(*) FROM artifacts"
            ).fetchone()
            if (
                count + len(prepared) > self.max_objects
                or total + sum(item.size for item in prepared) > self.capacity
            ):
                raise WorkspaceError("workspace_full")
            identities = []
            for item in prepared:
                self._publish_file(item)
                identity, now = str(uuid4()), time.time()
                db.execute(
                    "INSERT INTO artifacts "
                    "(id,name,blob,sha256,size,revision,created_at,modified_at,expires_at) "
                    "VALUES (?,?,?,?,?,1,?,?,?)",
                    (
                        identity,
                        item.name,
                        item.blob,
                        item.sha256,
                        item.size,
                        now,
                        now,
                        now + self.ttl if self.ttl else 253402300799,
                    ),
                )
                db.execute("INSERT INTO artifact_snapshots VALUES (?)", (identity,))
                identities.append(identity)
            return [self._metadata(self._row(db, identity)) for identity in identities]

    def publish_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        prepared = self.prepare_batch(files)
        try:
            return self.publish_prepared_batch(prepared)
        finally:
            self.discard_prepared(prepared)
            self.cleanup()

    def read_bytes(
        self, artifact_id: str, *, max_bytes: int | None = None
    ) -> tuple[dict[str, Any], bytes]:
        with ExitStack() as resources:
            with self._transaction(write=False) as db:
                row = self._row(db, artifact_id)
                limit = self.max_file if max_bytes is None else min(self.max_file, max_bytes)
                if limit < 0 or row["size"] > limit:
                    raise WorkspaceError("artifact_too_large")
                path = self._blob(row["blob"])
                if path.is_symlink():
                    raise WorkspaceError("unsafe_artifact")
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                stream = resources.enter_context(os.fdopen(descriptor, "rb"))
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > self.max_file
                ):
                    raise WorkspaceError("unsafe_artifact")
                if info.st_size > limit:
                    raise WorkspaceError("artifact_too_large")
                metadata = self._metadata(row)
            # The open FD pins the selected immutable blob after releasing SQLite.
            data = stream.read(limit + 1)
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise WorkspaceError("artifact_corrupt")
            return metadata, data

    def snapshot(
        self, descriptor: int, name: str, *, artifact_id: str | None = None
    ) -> dict[str, Any]:
        """Stream an already safely opened workspace file into an immutable artifact."""
        if not name or len(name) > 128 or any(c in name for c in "\\/:\x00"):
            raise WorkspaceError("invalid_artifact_name")
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > self.max_file:
            raise WorkspaceError("artifact_too_large")
        identity, blob = self._id(artifact_id) if artifact_id else str(uuid4()), f"{uuid4()}.blob"
        with self._transaction(write=False) as db:
            if (
                artifact_id
                and db.execute("SELECT 1 FROM artifacts WHERE id=?", (identity,)).fetchone()
            ):
                existing = self._row(db, identity)
                if not existing["immutable"]:
                    raise WorkspaceError("snapshot_identity_conflict")
                return self._metadata(existing)
        path = self.root / f"{uuid4()}.pending"
        digest = hashlib.sha256()
        total = 0
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            with path.open("xb") as output:
                while chunk := os.read(descriptor, 65536):
                    total += len(chunk)
                    if total > self.max_file:
                        raise WorkspaceError("artifact_too_large")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise WorkspaceError("file_changed_during_publish")
            with self._transaction() as db:
                self._prune_metadata(db)
                if db.execute("SELECT 1 FROM artifacts WHERE id=?", (identity,)).fetchone():
                    existing = self._row(db, identity)
                    if not existing["immutable"]:
                        raise WorkspaceError("snapshot_identity_conflict")
                    result = self._metadata(existing)
                else:
                    size, count = db.execute(
                        "SELECT coalesce(sum(size),0),count(*) FROM artifacts"
                    ).fetchone()
                    if size + total > self.capacity or count >= self.max_objects:
                        raise WorkspaceError("workspace_full")
                    self._publish_file(
                        PreparedArtifact(name, path, blob, digest.hexdigest(), total)
                    )
                    now = time.time()
                    db.execute(
                        "INSERT INTO artifacts "
                        "(id,name,blob,sha256,size,revision,created_at,modified_at,expires_at) "
                        "VALUES (?,?,?,?,?,1,?,?,?)",
                        (
                            identity,
                            name,
                            blob,
                            digest.hexdigest(),
                            total,
                            now,
                            now,
                            now + self.ttl if self.ttl else 253402300799,
                        ),
                    )
                    db.execute("INSERT INTO artifact_snapshots VALUES (?)", (identity,))
                    result = self._metadata(self._row(db, identity))
            return result
        except BaseException:
            self._unlink([self._blob(blob)])
            raise
        finally:
            self._unlink([path])

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

    def list(
        self, *, cursor: str = "", limit: int = 20, number: int | None = None
    ) -> dict[str, Any]:
        if cursor:
            self._id(cursor)
        if not 1 <= limit <= 100:
            raise WorkspaceError("invalid_limit")
        if number is not None and (
            type(number) is not int or not 1 <= number <= 1_000_000 or cursor
        ):
            raise WorkspaceError("invalid_page_number")
        with self._transaction(write=False) as db:
            now = time.time()
            if number is not None:
                total = int(
                    db.execute(
                        "SELECT count(*) FROM artifacts WHERE expires_at>?", (now,)
                    ).fetchone()[0]
                )
                rows = db.execute(
                    "SELECT *, EXISTS(SELECT 1 FROM artifact_snapshots s WHERE s.id=artifacts.id) "
                    "AS immutable FROM artifacts WHERE expires_at>? "
                    "ORDER BY modified_at DESC, id DESC LIMIT ? OFFSET ?",
                    (now, limit, (number - 1) * limit),
                ).fetchall()
                return {
                    "items": [self._metadata(row) for row in rows],
                    "next_cursor": None,
                    "total": total,
                    "number": number,
                }
            rows = db.execute(
                "SELECT *, EXISTS(SELECT 1 FROM artifact_snapshots s WHERE s.id=artifacts.id) "
                "AS immutable FROM artifacts WHERE expires_at>? AND id>? ORDER BY id LIMIT ?",
                (now, cursor, limit + 1),
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
