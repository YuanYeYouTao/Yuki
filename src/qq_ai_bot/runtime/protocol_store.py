"""Immutable private protocol objects beside the runtime database.

Only a small manifest is published under the Work lease. Objects are written
before that transaction; orphan objects are safe to retain until maintenance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path
from typing import Any, cast

from sqlalchemy import and_, delete, or_, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
from qq_ai_bot.domain.messages import ChatMessage, FunctionCallOutput
from qq_ai_bot.execution_trace.phases import timed_lock
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.protocol_schema import objects, refs, usage

_SNAPSHOT_MAGIC = b"YUKI-CODE-SNAPSHOT\x00\x01"


@dataclass(frozen=True, slots=True)
class CodeSnapshotBinding:
    """Host provenance checked against the original owner before native loading."""

    work_id: str
    operation_id: str
    source_execution_id: str
    conversation_id: str
    generation: int
    source_revision: int
    privacy_generation: int
    engine_digest: str
    api_revision: str
    dump_format: str


_CACHE_ENTRIES = 1024  # Metadata only; never retain encoded transcript copies.
_CACHE_RECORD_BYTES = 2 * 1024 * 1024  # Bound the retained immutable source records too.
_GC_BRANCH_ROWS = 64  # Two indexed branches visit at most 128 metadata rows per pass.
_GC_FILE_SECONDS = 0.1  # Yield after the current filesystem operation completes.
FileIdentity = tuple[int, int, int, int, int]


async def _finish_thread[T](call: Callable[..., T], *args: Any) -> T:
    """Cancellation must not release a file fence while its worker still runs."""
    worker = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as original:
        # A second cancellation must not let an unfinished unlink escape the lock.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break  # Retrieve the worker failure below without replacing cancellation.
        try:
            worker.result()
        except Exception as cleanup:
            original.add_note(f"protocol_thread_cleanup_failed:{type(cleanup).__name__}")
        raise


@dataclass
class _GCScan:
    cutoff: float
    high_water: tuple[float, str]
    cursor: tuple[float, str] | None = None


class ProtocolStore:
    def __init__(
        self, database: Database, *, policy: WorkStorageRuntimeConfig | None = None
    ) -> None:
        self.database = database
        self.policy = policy or WorkStorageRuntimeConfig()
        self.prepared_refs: set[str] = set()
        self.prepared_sizes: dict[str, int] = {}
        self._prepared_sources: dict[str, Callable[[], bytes]] = {}
        self._identities: OrderedDict[str, FileIdentity] = OrderedDict()
        self._records: OrderedDict[int, tuple[ChatMessage | FunctionCallOutput, str, int]] = (
            OrderedDict()
        )
        self._record_chain: tuple[str, str] | None = None
        self._record_bytes = 0
        if getattr(database, "_protocol_storage_lock", None) is None:
            database._protocol_storage_lock = asyncio.Lock()
        assert database._protocol_storage_lock is not None
        self._lock = database._protocol_storage_lock
        if getattr(database, "_protocol_gc_lock", None) is None:
            database._protocol_gc_lock = asyncio.Lock()
        assert database._protocol_gc_lock is not None
        self._gc_lock = database._protocol_gc_lock
        self._scans = cast(dict[bool, _GCScan], database._protocol_gc_scans)
        database_path = make_url(database.url).database
        if not database_path or database_path == ":memory:":
            root = getattr(database, "_protocol_store_path", None)
            if root is None:
                root = Path(tempfile.mkdtemp(prefix="yuki-protocol-"))
                database._protocol_store_path = root
        else:
            root = Path(database_path).resolve().parent / "work-protocol"
        self.root = Path(root)

    async def refresh_policy(self) -> None:
        """Resolve one global snapshot before file preparation, never per object/write."""
        resolver = self.database.protocol_storage_policy
        if resolver is not None:
            self.policy = await resolver()

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid_protocol_reference")
        return self.root / digest[:2] / digest[2:]

    async def put(self, value: Any) -> str:
        content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return await self.put_bytes(content)

    async def put_bytes(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        if digest not in self.prepared_refs:
            async with timed_lock(self._lock, "protocol"):
                await _finish_thread(self._publish, digest, content)
        self.prepared_refs.add(digest)
        self.prepared_sizes[digest] = len(content)
        self._prepared_sources[digest] = lambda: content
        return digest

    def begin_record_chain(self, work_id: str, chain_id: str) -> None:
        if self._record_chain != (work_id, chain_id):
            self.clear_record_cache()
            self._record_chain = (work_id, chain_id)

    def clear_record_cache(self) -> None:
        self._records.clear()
        self._record_bytes = 0
        self._record_chain = None

    async def put_record(self, record: ChatMessage | FunctionCallOutput) -> str:
        """Only deeply immutable, non-media ordinary records can skip encoding."""
        if not self.cacheable_record(record):
            raise ValueError("work_protocol_record_not_cacheable")
        key = id(record)
        cached = self._records.get(key)
        if cached is not None and cached[0] is record:
            _, digest, size = cached
            self._records.move_to_end(key)
            self.prepared_refs.add(digest)
            self.prepared_sizes[digest] = size
        else:
            digest = await self.put(self._encoded_record(record))
            size = self.prepared_sizes[digest]
            if size <= _CACHE_RECORD_BYTES:
                self._records[key] = (record, digest, size)
                self._record_bytes += size
                while (
                    len(self._records) > _CACHE_ENTRIES or self._record_bytes > _CACHE_RECORD_BYTES
                ):
                    _, (_, _, evicted_size) = self._records.popitem(last=False)
                    self._record_bytes -= evicted_size
        self._prepared_sources[digest] = lambda: json.dumps(
            self._encoded_record(record), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        return digest

    @staticmethod
    def _encoded_record(record: ChatMessage | FunctionCallOutput) -> dict[str, Any]:
        return {
            "kind": "message" if isinstance(record, ChatMessage) else "result",
            "value": asdict(record),
        }

    @staticmethod
    def cacheable_record(record: ChatMessage | FunctionCallOutput) -> bool:
        values: tuple[str | None, ...]
        if isinstance(record, FunctionCallOutput):
            values = (record.call_id, record.output)
        else:
            if record.response_item is not None or record.images:
                return False
            values = (
                record.role,
                record.content,
                record.tool_call_id,
                record.reasoning_content,
                *(
                    value
                    for call in record.tool_calls
                    for value in (call.id, call.type, call.function.name, call.function.arguments)
                ),
            )
        return not any(value is not None and value.startswith("data:image/") for value in values)

    @staticmethod
    def _identity(path: Path) -> FileIdentity:
        info = path.stat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _verify(self, digest: str) -> FileIdentity:
        target = self._path(digest)
        identity = self._identity(target)
        if self._identities.get(digest) != identity:
            content = target.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest or self._identity(target) != identity:
                raise ValueError("work_protocol_object_corrupt")
        self._identities[digest] = identity
        self._identities.move_to_end(digest)
        if len(self._identities) > _CACHE_ENTRIES:
            self._identities.popitem(last=False)
        return identity

    def _verify_prepared(self, digests: tuple[str, ...]) -> None:
        for digest in digests:
            try:
                identity = self._verify(digest)
            except FileNotFoundError:
                source = self._prepared_sources.get(digest)
                if source is None:
                    raise ValueError("work_protocol_object_missing_source") from None
                content = source()
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError("work_protocol_object_corrupt") from None
                self._publish(digest, content)
                identity = self._verify(digest)
            if identity[2] != self.prepared_sizes[digest]:
                raise ValueError("work_protocol_object_corrupt")

    def _publish(self, digest: str, content: bytes) -> None:
        target = self._path(digest)
        if target.exists():
            self._verify(digest)
            return
        if len(content) > self.policy.object_max_bytes:
            raise ValueError("work_protocol_object_capacity")
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < len(content) + self.policy.disk_reserve_bytes:
            raise ValueError("work_protocol_storage_capacity")
        descriptor, temporary = tempfile.mkstemp(prefix=".publishing-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._verify(digest)

    async def get(self, digest: str) -> Any:
        return json.loads(await self.get_bytes(digest))

    async def get_bytes(self, digest: str) -> bytes:
        content = await asyncio.to_thread(self._path(digest).read_bytes)
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("work_protocol_object_corrupt")
        return content

    async def put_code_snapshot(self, binding: CodeSnapshotBinding, dump: bytes) -> str:
        """T0 binary preparation; publication still uses original Work refs/GC."""
        header = json.dumps(asdict(binding), ensure_ascii=False, allow_nan=False).encode()
        return await self.put_bytes(
            _SNAPSHOT_MAGIC + len(header).to_bytes(4, "big") + header + dump
        )

    async def get_code_snapshot(self, digest: str, binding: CodeSnapshotBinding) -> bytes:
        """A public artifact/path or a mismatched runtime cannot supply a dump."""
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
        from qq_ai_bot.runtime.work_schema_v1 import work

        async with self.database.sessions() as reader:
            authority = await reader.scalar(
                select(work.c.id)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(
                    work.c.id == binding.work_id,
                    work.c.conversation_id == binding.conversation_id,
                    work.c.generation == binding.generation,
                    work.c.state.not_in(("completed", "failed", "cancelled")),
                    CanonicalConversationModel.generation == binding.generation,
                    CanonicalConversationModel.prompt_source_revision == binding.source_revision,
                )
            )
            privacy = (
                await reader.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1,
                    )
                )
                or 0
            )
            if authority is None or privacy != binding.privacy_generation:
                raise ValueError("code_snapshot_authority_changed")
            owned = await reader.scalar(
                select(refs.c.sha256).where(
                    refs.c.work_id == binding.work_id,
                    refs.c.sha256 == digest,
                    refs.c.sha256.in_(
                        select(objects.c.sha256).where(objects.c.deleting.is_(False))
                    ),
                )
            )
        if owned is None:
            raise ValueError("code_snapshot_not_owned")
        content = await self.get_bytes(digest)
        return self.decode_code_snapshot(content, binding)

    @staticmethod
    def decode_code_snapshot(content: bytes, binding: CodeSnapshotBinding) -> bytes:
        offset = len(_SNAPSHOT_MAGIC)
        if not content.startswith(_SNAPSHOT_MAGIC) or len(content) < offset + 4:
            raise ValueError("code_snapshot_format_mismatch")
        header_size = int.from_bytes(content[offset : offset + 4], "big")
        offset += 4
        if header_size > 8192 or offset + header_size > len(content):
            raise ValueError("code_snapshot_header_invalid")
        try:
            header = json.loads(content[offset : offset + header_size])
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("code_snapshot_header_invalid") from exc
        if header != asdict(binding):
            raise ValueError("code_snapshot_binding_mismatch")
        return content[offset + header_size :]

    async def manifest(
        self,
        payload: dict[str, Any],
        *,
        refresh_policy: bool = True,
        item_digests: tuple[str | None, ...] = (),
    ) -> dict[str, Any]:
        if refresh_policy:
            await self.refresh_policy()
        transcript = dict(payload["transcript"])
        transcript["items"] = [
            item_digests[index]
            if item_digests and item_digests[index] is not None
            else await self.put(item)
            for index, item in enumerate(transcript["items"])
        ]
        transcript["continuation"] = (
            await self.put(transcript["continuation"]) if transcript["continuation"] else None
        )
        metadata = dict(payload["metadata"])
        progress = dict(metadata.get("progress", {}))
        if "model_observations" in progress:
            progress["model_observation_refs"] = [
                await self.put(value) for value in progress.pop("model_observations")
            ]
            metadata["progress"] = progress
        return {
            "format": "protocol_objects_v1",
            "transcript": transcript,
            "pending_ref": await self.put(payload["pending"]),
            "metadata_ref": await self.put(metadata),
            "file_media": payload.get("file_media", []),
        }

    async def hydrate(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if manifest.get("format") != "protocol_objects_v1":
            return manifest  # Existing journals migrate on their next paired save.
        transcript = dict(manifest["transcript"])
        transcript["items"] = [await self.get(digest) for digest in transcript["items"]]
        if transcript["continuation"]:
            transcript["continuation"] = await self.get(transcript["continuation"])
        metadata = await self.get(manifest["metadata_ref"])
        progress = metadata.get("progress", {})
        if "model_observation_refs" in progress:
            progress["model_observations"] = [
                await self.get(digest) for digest in progress.pop("model_observation_refs")
            ]
        return {
            "transcript": transcript,
            "pending": await self.get(manifest["pending_ref"]),
            "metadata": metadata,
            "file_media": manifest.get("file_media", []),
        }

    @asynccontextmanager
    async def publication(self, work_id: str) -> AsyncIterator[tuple[dict[str, Any], ...]]:
        """Prepare bounded rows before acquiring SQLite's writer fence."""
        async with timed_lock(self._lock, "protocol"):
            published = False
            try:
                digests = tuple(sorted(self.prepared_refs))
                await _finish_thread(self._verify_prepared, digests)
                owned: set[str] = set()
                async with self.database.sessions() as reader:
                    for offset in range(0, len(digests), 256):
                        owned.update(
                            await reader.scalars(
                                select(refs.c.sha256).where(
                                    refs.c.work_id == work_id,
                                    refs.c.sha256.in_(digests[offset : offset + 256]),
                                )
                            )
                        )
                yield tuple(
                    {
                        "sha256": digest,
                        "byte_size": self.prepared_sizes[digest],
                        "prepared_at": time.time(),
                        "deleting": False,
                    }
                    for digest in digests
                    if digest not in owned
                )
                published = True
            finally:
                # Extra protocol objects may be prepared outside this manifest
                # (e.g. the pre-compaction chain). Retain their bounded metadata
                # after failure, but never keep transient encoded bytes for retries.
                if published:
                    self.prepared_refs.clear()
                    self.prepared_sizes.clear()
                self._prepared_sources.clear()

    async def publish_refs(
        self, session: AsyncSession, work_id: str, prepared: tuple[dict[str, Any], ...]
    ) -> None:
        """Batch metadata/ref CAS under the journal's original Work lease."""
        added_objects = False
        for offset in range(0, len(prepared), 128):
            batch = prepared[offset : offset + 128]
            digests = tuple(item["sha256"] for item in batch)
            added_sizes = tuple(
                await session.scalars(
                    insert(objects).on_conflict_do_nothing().returning(objects.c.byte_size), batch
                )
            )
            if any(size > self.policy.object_max_bytes for size in added_sizes):
                raise ValueError("work_protocol_object_capacity")
            added_objects = added_objects or bool(added_sizes)
            live = set(
                await session.scalars(
                    select(objects.c.sha256).where(
                        objects.c.sha256.in_(digests),
                        objects.c.deleting.is_(False),
                    )
                )
            )
            if live != set(digests):
                raise ValueError("work_protocol_reference_deleting")
            await session.execute(
                insert(refs).on_conflict_do_nothing(),
                [{"work_id": work_id, "sha256": digest} for digest in digests],
            )
        if added_objects:
            used = await session.scalar(select(usage.c.byte_size).where(usage.c.id == 1))
            if used is None or used > self.policy.total_max_bytes:
                raise ValueError("work_protocol_storage_capacity")

    def _orphan_candidates(self, cutoff: float) -> list[dict[str, Any]]:
        iterator = getattr(self.database, "_protocol_gc_iterator", None)
        if iterator is None:
            iterator = self.root.glob("*/*")
            self.database._protocol_gc_iterator = iterator
        candidates = list(islice(iterator, 128))
        if len(candidates) < 128:
            self.database._protocol_gc_iterator = None
        result = []
        for path in candidates:
            digest = path.parent.name + path.name
            try:
                if self._path(digest) != path:
                    continue
                info = path.stat()
                if info.st_mtime < cutoff and path.is_file():
                    result.append(
                        {
                            "sha256": digest,
                            "byte_size": info.st_size,
                            "prepared_at": info.st_mtime,
                            "deleting": True,
                        }
                    )
            except (OSError, ValueError):
                continue
        return result

    async def _gc_page(self, deleting: bool, cutoff: float) -> list[dict[str, Any]]:
        """Page metadata before examining ownership, even when every row is owned."""
        scan = self._scans.get(deleting)
        async with self.database.sessions() as reader:
            if scan is None:
                conditions: list[ColumnElement[bool]] = [objects.c.deleting.is_(deleting)]
                if not deleting:
                    conditions.append(objects.c.prepared_at < cutoff)
                high = (
                    await reader.execute(
                        select(objects.c.prepared_at, objects.c.sha256)
                        .where(*conditions)
                        .order_by(objects.c.prepared_at.desc(), objects.c.sha256.desc())
                        .limit(1)
                    )
                ).first()
                if high is None:
                    return []
                scan = _GCScan(cutoff, (high.prepared_at, high.sha256))
                self._scans[deleting] = scan
            key = tuple_(objects.c.prepared_at, objects.c.sha256)
            conditions = [objects.c.deleting.is_(deleting), key <= scan.high_water]
            if not deleting:
                conditions.append(objects.c.prepared_at < scan.cutoff)
            if scan.cursor is not None:
                conditions.append(key > scan.cursor)
            page = list(
                (
                    await reader.execute(
                        select(objects)
                        .where(*conditions)
                        .order_by(objects.c.prepared_at, objects.c.sha256)
                        .limit(_GC_BRANCH_ROWS)
                    )
                ).mappings()
            )
            if page:
                last = page[-1]
                scan.cursor = (last["prepared_at"], last["sha256"])
            if len(page) < _GC_BRANCH_ROWS or scan.cursor == scan.high_water:
                self._scans.pop(deleting, None)
            if not page:
                return []
            owned = set(
                await reader.scalars(
                    select(refs.c.sha256).where(refs.c.sha256.in_([row["sha256"] for row in page]))
                )
            )
            return [dict(row) for row in page if row["sha256"] not in owned]

    def _unlink_objects(self, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The caller holds only the file fence; finish a current unlink before yielding."""
        removed = []
        started = time.monotonic()
        for row in selected:
            digest = row["sha256"]
            succeeded = False
            try:
                identity = self._verify(digest)
                if identity[2] == row["byte_size"]:
                    self._path(digest).unlink()
                    succeeded = True
            except FileNotFoundError:
                succeeded = True  # A prior unlink may have finished before its acknowledgement.
            except (OSError, ValueError):
                pass
            if succeeded:
                self._identities.pop(digest, None)
                removed.append(row)
            if time.monotonic() - started >= _GC_FILE_SECONDS:
                break
        return removed

    async def cleanup(self, *, grace_seconds: float = 86400) -> int:
        """CAS deletion barriers, briefly fence files, then batch durable acknowledgement."""
        async with self._gc_lock:
            cutoff = time.time() - grace_seconds
            # Directory discovery is bounded and cannot authorize a later unlink.
            orphans = await _finish_thread(self._orphan_candidates, cutoff)
            if orphans:
                async with self.database.sessions() as reader:
                    registered = set(
                        await reader.scalars(
                            select(objects.c.sha256).where(
                                objects.c.sha256.in_(tuple(item["sha256"] for item in orphans)),
                            )
                        )
                    )
                orphaned = [item for item in orphans if item["sha256"] not in registered]
                if orphaned:
                    async with self.database.immediate_session() as writer:
                        current_cutoff = time.time() - grace_seconds
                        still_expired = [
                            item for item in orphaned if item["prepared_at"] < current_cutoff
                        ]
                        if still_expired:
                            await writer.execute(
                                insert(objects).on_conflict_do_nothing(), still_expired
                            )
            unowned = ~select(refs.c.sha256).where(refs.c.sha256 == objects.c.sha256).exists()
            candidates = [*await self._gc_page(True, cutoff), *await self._gc_page(False, cutoff)]
            if not candidates:
                return 0
            frozen = or_(
                *(
                    and_(
                        objects.c.sha256 == row["sha256"],
                        objects.c.prepared_at == row["prepared_at"],
                        objects.c.byte_size == row["byte_size"],
                        objects.c.deleting.is_(row["deleting"]),
                    )
                    for row in candidates
                )
            )
            async with self.database.immediate_session() as writer:
                current_cutoff = time.time() - grace_seconds
                selected = list(
                    (
                        await writer.execute(
                            update(objects)
                            .where(
                                frozen,
                                unowned,
                                or_(
                                    objects.c.deleting.is_(True),
                                    objects.c.prepared_at < current_cutoff,
                                ),
                            )
                            .values(deleting=True)
                            .returning(objects)
                        )
                    ).mappings()
                )
            if not selected:
                return 0
            # Never await SQLite's writer while holding the GC file fence.
            async with timed_lock(self._lock, "protocol"):
                removed = await _finish_thread(
                    self._unlink_objects, [dict(row) for row in selected]
                )
            if not removed:
                return 0
            frozen_removed = or_(
                *(
                    and_(
                        objects.c.sha256 == row["sha256"],
                        objects.c.prepared_at == row["prepared_at"],
                        objects.c.byte_size == row["byte_size"],
                    )
                    for row in removed
                )
            )
            async with self.database.immediate_session() as writer:
                deleted = list(
                    await writer.scalars(
                        delete(objects)
                        .where(frozen_removed, objects.c.deleting.is_(True), unowned)
                        .returning(objects.c.sha256)
                    )
                )
            return len(deleted)
