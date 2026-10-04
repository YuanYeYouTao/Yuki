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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
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


class ProtocolStore:
    def __init__(
        self, database: Database, *, policy: WorkStorageRuntimeConfig | None = None
    ) -> None:
        self.database = database
        self.policy = policy or WorkStorageRuntimeConfig()
        self.prepared_refs: set[str] = set()
        self.prepared_sizes: dict[str, int] = {}
        if getattr(database, "_protocol_storage_lock", None) is None:
            database._protocol_storage_lock = asyncio.Lock()
        assert database._protocol_storage_lock is not None
        self._lock = database._protocol_storage_lock
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
        if digest not in self.prepared_refs or not await asyncio.to_thread(
            self._path(digest).is_file
        ):
            async with self._lock:
                await asyncio.to_thread(self._publish, digest, content)
            self.prepared_refs.add(digest)
            self.prepared_sizes[digest] = len(content)
        return digest

    def _publish(self, digest: str, content: bytes) -> None:
        target = self._path(digest)
        if target.exists():
            return  # Read/backup verifies full hashes; repeated save reuses immutable bytes.
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
        self, payload: dict[str, Any], *, refresh_policy: bool = True
    ) -> dict[str, Any]:
        if refresh_policy:
            await self.refresh_policy()
        transcript = dict(payload["transcript"])
        transcript["items"] = [await self.put(item) for item in transcript["items"]]
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
        async with self._lock:
            digests = tuple(sorted(self.prepared_refs))
            if not await asyncio.to_thread(lambda: all(self._path(d).is_file() for d in digests)):
                raise ValueError("work_protocol_reference_deleting")
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
            self.prepared_refs.clear()
            self.prepared_sizes.clear()

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

    async def cleanup(self, *, grace_seconds: float = 86400) -> int:
        """CAS unowned objects, unlink outside SQLite, then remove metadata."""
        removed = 0
        async with self._lock:
            orphans = await asyncio.to_thread(self._orphan_candidates, time.time() - grace_seconds)
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
                        await writer.execute(insert(objects).on_conflict_do_nothing(), orphaned)
            unowned = ~select(refs.c.sha256).where(refs.c.sha256 == objects.c.sha256).exists()
            eligible = unowned & or_(
                objects.c.deleting.is_(True), objects.c.prepared_at < time.time() - grace_seconds
            )
            async with self.database.sessions() as reader:
                candidates = list(
                    await reader.scalars(
                        select(objects.c.sha256)
                        .where(eligible)
                        .order_by(objects.c.prepared_at)
                        .limit(128)
                    )
                )
            if not candidates:
                return 0
            async with self.database.immediate_session() as writer:
                selected = list(
                    await writer.scalars(
                        update(objects)
                        .where(
                            objects.c.sha256.in_(candidates),
                            eligible,
                        )
                        .values(deleting=True)
                        .returning(objects.c.sha256)
                    )
                )
            for digest in selected:
                try:
                    await asyncio.to_thread(self._path(digest).unlink, missing_ok=True)
                except OSError:
                    continue
                async with self.database.immediate_session() as writer:
                    deleted = await writer.scalar(
                        delete(objects)
                        .where(
                            objects.c.sha256 == digest,
                            objects.c.deleting.is_(True),
                            unowned,
                        )
                        .returning(objects.c.sha256)
                    )
                    removed += int(deleted is not None)
        return removed
