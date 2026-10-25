"""Application-owned tool result artifacts and retained research material."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from heapq import nsmallest
from itertools import islice
from pathlib import Path
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.capabilities.media import PreparedMediaData
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatImage
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ToolArtifactModel,
)
from yuki.tool_results.access import ArtifactAccess, ArtifactRead
from yuki.tool_results.json_length import escaped_character_length, json_character_count

_PRIVATE_MEDIA_TYPE = "application/x-yuki-prepared-images"


class ToolArtifactRepository:
    """Store complete oversized results in bounded files, not SQLite."""

    def __init__(
        self,
        database: Database,
        root: Path,
        *,
        retention_seconds: int,
        max_artifact_bytes: int = 64 * 1024 * 1024,
        max_total_bytes: int = 512 * 1024 * 1024,
        max_media_bytes: int = 6_291_456,
        max_media_frames: int = 16,
    ) -> None:
        if retention_seconds <= 0 or max_media_bytes <= 0 or max_media_frames <= 0:
            raise ValueError("artifact retention must be positive")
        self._database = database
        self._root = root
        self._retention = retention_seconds
        self._max_artifact_bytes = max_artifact_bytes
        self._max_total_bytes = max_total_bytes
        self._max_media_bytes = max_media_bytes
        self._max_media_frames = max_media_frames
        self._storage_lock = asyncio.Lock()
        self._orphan_iterator: Iterator[Path] | None = None
        # One application-owned store is also used for durable Work receipts.
        database.work_result_store = self

    @staticmethod
    def _protected() -> Any:
        from yuki.runtime.work_schema_v1 import work
        from yuki.runtime.work_tree import ancestors
        from yuki.tool_results.schema import artifact_refs

        cutoff = datetime.now(UTC).timestamp() - 7 * 86400
        retained = or_(
            work.c.state.not_in(("completed", "failed", "cancelled")), work.c.updated >= cutoff
        )
        ownership = (
            select(work.c.id)
            .where(work.c.id.in_(ancestors(ToolArtifactModel.work_id, include_self=True)), retained)
            .exists()
        )
        references = (
            select(artifact_refs.c.handle_id)
            .where(
                artifact_refs.c.handle_id == ToolArtifactModel.handle_id,
                or_(
                    artifact_refs.c.owner_kind != "work_note",
                    select(work.c.id)
                    .where(work.c.id == artifact_refs.c.owner_id, retained)
                    .exists(),
                ),
            )
            .exists()
        )
        return or_(ownership, references)

    @staticmethod
    async def add_refs(
        session: AsyncSession, owner_kind: str, owner_id: str, handles: tuple[str, ...]
    ) -> None:
        """Publish prepared references in their owner's existing short transaction."""
        from sqlalchemy.dialects.sqlite import insert

        from yuki.tool_results.schema import artifact_refs

        if owner_kind not in {"work_note", "observation", "summary"} or not owner_id:
            raise ValueError("artifact_reference_owner_invalid")
        for handle in dict.fromkeys(handles):
            if not await session.scalar(
                select(ToolArtifactModel.handle_id).where(
                    ToolArtifactModel.handle_id == handle,
                    ToolArtifactModel.deleting.is_(False),
                    or_(
                        ToolArtifactModel.expires_at > datetime.now(UTC),
                        ToolArtifactRepository._protected(),
                    ),
                )
            ):
                raise ValueError("artifact_reference_unavailable")
            await session.execute(
                insert(artifact_refs)
                .values(owner_kind=owner_kind, owner_id=owner_id, handle_id=handle)
                .on_conflict_do_nothing()
            )

    @staticmethod
    async def _authorized(
        session: AsyncSession, row: ToolArtifactModel, access: ArtifactAccess
    ) -> bool:
        conversation = await session.get(CanonicalConversationModel, access.conversation_id)
        if conversation is None or conversation.generation != access.generation:
            return False
        if row.access_json:
            source = json.loads(row.access_json)
            privacy = int(
                await session.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
            if source.get("privacy_generation") != privacy:
                return False
            from yuki.identity.person_identity import same_person_id

            actor_matches = source.get("actor_person_id") == access.actor_person_id or (
                access.principal_kind == "person"
                and bool(source.get("actor_person_id"))
                and await same_person_id(session, source["actor_person_id"], access.actor_person_id)
            )
            return actor_matches and all(
                source.get(key) == value
                for key, value in (
                    ("conversation_id", access.conversation_id),
                    ("generation", access.generation),
                    ("principal_kind", access.principal_kind),
                    ("read_scope", access.read_scope),
                )
            )
        # A legacy handle is not a public bearer capability. Its original Work
        # remains the trusted source; unowned old results cannot grant access.
        if row.work_id is None:
            return False
        from yuki.runtime.work_schema_v1 import work

        original = (
            await session.execute(
                select(work.c.source_json, work.c.conversation_id, work.c.generation).where(
                    work.c.id == row.work_id
                )
            )
        ).first()
        if (
            original is None
            or original.conversation_id != access.conversation_id
            or (original.generation != access.generation)
        ):
            return False
        source = json.loads(original.source_json)
        from yuki.identity.person_identity import same_person_id

        return bool(
            (
                source.get("actor_person_id") == access.actor_person_id
                or (
                    access.principal_kind == "person"
                    and source.get("actor_person_id")
                    and await same_person_id(
                        session, source["actor_person_id"], access.actor_person_id
                    )
                )
            )
            and source.get("principal_kind", "person") == access.principal_kind
            and source.get("read_scope", "") == access.read_scope
        )

    def configure_retention(self, retention_seconds: int) -> None:
        if retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        self._retention = retention_seconds

    async def write_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        content: str | bytes,
        media_type: str,
        retention_seconds: int | None = None,
        work_id: str | None = None,
        effect_key: str | None = None,
        access: ArtifactAccess | None = None,
        source_event_ids: tuple[int, ...] = (),
        source_privacy_generation: int | None = None,
    ) -> str:
        from yuki.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None:
            source_event_ids = tuple(dict.fromkeys((*source_event_ids, *capture.source_event_ids)))
            if capture.source_privacy_generation is not None:
                source_privacy_generation = (
                    capture.source_privacy_generation
                    if source_privacy_generation is None
                    else min(source_privacy_generation, capture.source_privacy_generation)
                )
        if capture is not None and capture.work_id and capture.effect_key and work_id is None:
            work_id, effect_key = capture.work_id, capture.effect_key
        handle = uuid.uuid4().hex
        relative = f"{handle}.json"
        path = self._root / relative
        encoded = content if isinstance(content, bytes) else content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if len(encoded) > self._max_artifact_bytes:
            raise ValueError("tool_artifact_capacity")
        retention = retention_seconds if retention_seconds is not None else self._retention
        if retention <= 0:
            raise ValueError("artifact retention must be positive")
        async with self._database.read_session() as reader:
            privacy = int(
                await reader.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
        if source_privacy_generation is not None:
            privacy = source_privacy_generation
        source = json.loads(access.encode(privacy)) if access is not None else {}
        if source_event_ids:
            source["source_event_ids"] = list(source_event_ids)
            source["privacy_generation"] = privacy
        access_json = json.dumps(source, sort_keys=True) if source else None
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        async with self._storage_lock:
            async with self._database.read_session() as reader:
                used = await reader.scalar(
                    select(func.coalesce(func.sum(ToolArtifactModel.byte_size), 0))
                )
            if int(used or 0) + len(encoded) > self._max_total_bytes:
                raise ValueError("tool_artifact_capacity")
            await self._publish_file(path, encoded)
            # Cancellation can arrive after COMMIT: bounded orphan maintenance
            # owns unregistered files; never unlink a potentially durable ref here.
            async with self._database.immediate_session() as session:
                now = datetime.now(UTC)
                if access is not None or source_event_ids:
                    if (
                        access is not None
                        and not await session.scalar(
                            select(CanonicalConversationModel.id).where(
                                CanonicalConversationModel.id == access.conversation_id,
                                CanonicalConversationModel.generation == access.generation,
                            )
                        )
                    ) or int(
                        await session.scalar(
                            select(ExecutionTraceStateModel.privacy_generation).where(
                                ExecutionTraceStateModel.id == 1
                            )
                        )
                        or 0
                    ) != privacy:
                        raise ValueError("artifact_source_changed")
                if work_id is not None:
                    from yuki.runtime.work_schema_v1 import work

                    if not await session.scalar(select(work.c.id).where(work.c.id == work_id)):
                        raise ValueError("tool_artifact_work_unavailable")
                session.add(
                    ToolArtifactModel(
                        handle_id=handle,
                        provider_id=provider_id[:128],
                        tool_name=tool_name[:255],
                        relative_path=relative,
                        media_type=media_type[:128],
                        byte_size=len(encoded),
                        created_at=now,
                        expires_at=now + timedelta(seconds=retention),
                        work_id=work_id,
                        effect_key=effect_key,
                        access_json=access_json,
                        sha256=digest,
                        deleting=False,
                    )
                )
        return handle

    async def _publish_file(self, path: Path, encoded: bytes) -> None:
        worker = asyncio.create_task(asyncio.to_thread(self._publish, path, encoded))
        cancellation = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except BaseException as error:
                if cancellation is not None:
                    raise cancellation from error
                raise
        # Keep the storage owner until the actual filesystem operation exits.
        # A cancelled publication remains an unregistered UUID for orphan GC.
        try:
            worker.result()
        except BaseException as error:
            if cancellation is not None:
                raise cancellation from error
            raise
        if cancellation is not None:
            raise cancellation

    @staticmethod
    def _publish(path: Path, encoded: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".publishing-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _read_bounded(path: Path, maximum: int) -> bytes:
        with path.open("rb") as stream:
            return stream.read(maximum + 1)

    async def write_media_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        images: tuple[ChatImage, ...],
        access: ArtifactAccess | None,
        retention_seconds: int | None = None,
    ) -> str:
        """Archive authorized pixels privately under the existing artifact owner."""
        if access is None:
            raise ValueError("artifact_source_incomplete")
        if (
            not images
            or len(images) > self._max_media_frames
            or sum(len(i.data_url) for i in images) > self._max_media_bytes
        ):
            raise ValueError("artifact_media_capacity")
        content = json.dumps({"images": [asdict(image) for image in images]}, ensure_ascii=False)
        encoded = content.encode("utf-8")
        if len(encoded) > self._max_media_bytes + 65536:
            raise ValueError("artifact_media_capacity")
        return await self.write_artifact(
            provider_id=provider_id,
            tool_name=tool_name,
            content=encoded,
            media_type=_PRIVATE_MEDIA_TYPE,
            retention_seconds=retention_seconds,
            access=access,
        )

    async def validate_media(self, images: tuple[ChatImage, ...], access: ArtifactAccess) -> None:
        """Recheck the original handle's owner, expiry and bytes before dispatch."""
        for handle in dict.fromkeys(
            image.tool_handle for image in images if image.source == "tool"
        ):
            if not handle:
                raise ValueError("artifact_source_incomplete")
            prepared = await self.read(handle, operation="image", access=access)
            if not isinstance(prepared, PreparedMediaData):
                raise ValueError("artifact_media_source_changed")
            valid = {image.data_url for image in prepared.images}
            if any(
                image.data_url not in valid
                for image in images
                if image.source == "tool" and image.tool_handle == handle
            ):
                raise ValueError("artifact_media_source_changed")

    async def read(
        self,
        handle_id: str,
        *,
        operation: str = "text",
        path: tuple[str | int, ...] = (),
        offset: int = 0,
        limit: int = 8000,
        query: str = "",
        max_characters: int = 8000,
        item_limit: int | None = None,
        access: ArtifactAccess | None = None,
    ) -> dict[str, object] | None:
        if offset < 0 or limit <= 0 or max_characters <= 0:
            raise ValueError("artifact offset must be non-negative and limit must be positive")
        if not handle_id.isalnum() or len(handle_id) > 64:
            return None
        async with self._database.read_session() as session:
            row = await session.get(ToolArtifactModel, handle_id)
            if row is None or row.deleting:
                return None
            if access is not None and not await self._authorized(session, row, access):
                return _artifact_error("artifact_not_authorized", "Artifact 不属于当前获准读取范围")
            if _as_utc(row.expires_at) <= datetime.now(UTC) and not await session.scalar(
                select(ToolArtifactModel.handle_id).where(
                    ToolArtifactModel.handle_id == handle_id, self._protected()
                )
            ):
                return None
            relative = row.relative_path
            provider_id = row.provider_id
            tool_name = row.tool_name
            byte_size = row.byte_size
            digest = row.sha256
            media_type = row.media_type
            source = json.loads(row.access_json) if row.access_json else {}
            source_event_ids = tuple(source.get("source_event_ids", ()))
            source_privacy = source.get("privacy_generation")

        def sourced(data: dict[str, object]) -> dict[str, object]:
            return ArtifactRead(data, source_event_ids, source_privacy)

        if media_type == _PRIVATE_MEDIA_TYPE and access is None:
            return _artifact_error("artifact_not_authorized", "图片 Artifact 需要原读取授权")
        file_path = (self._root / relative).resolve()
        root = self._root.resolve()
        if root not in file_path.parents:
            return None

        def load() -> tuple[bool, dict[str, object]]:
            if operation == "text" and not query and media_type != _PRIVATE_MEDIA_TYPE:
                verified, page = _read_text_file(
                    file_path,
                    byte_size,
                    digest,
                    handle_id=handle_id,
                    offset=offset,
                    limit=limit,
                    max_characters=max_characters,
                )
                return verified, sourced(page) if verified else page
            try:
                raw = self._read_bounded(file_path, byte_size)
                if len(raw) != byte_size or (digest and hashlib.sha256(raw).hexdigest() != digest):
                    return False, _artifact_error("artifact_corrupt", "Artifact 完整性校验失败")
                content = raw.decode("utf-8")
                del raw
            except UnicodeDecodeError:
                return False, _artifact_error("artifact_corrupt", "Artifact 不是有效 UTF-8")
            except OSError:
                return False, _artifact_error(
                    "artifact_missing", "Artifact 正文缺失，执行回执仍然有效"
                )
            if media_type == _PRIVATE_MEDIA_TYPE:
                try:
                    decoded_media = json.loads(content)
                    del content
                    prepared_images = tuple(
                        replace(ChatImage(**item), source="tool", tool_handle=handle_id)
                        for item in decoded_media["images"]
                    )
                    if not prepared_images or len(prepared_images) > self._max_media_frames:
                        raise ValueError("invalid media count")
                except (ValueError, TypeError, KeyError, RecursionError):
                    return False, _artifact_error(
                        "artifact_corrupt", "图片 Artifact 完整性校验失败"
                    )
                manifest = {
                    "handle": handle_id,
                    "mode": "image",
                    "image_count": len(prepared_images),
                    "available_operations": ["inspect", "image"],
                    "provider_id": provider_id,
                    "tool_name": tool_name,
                }
                # Generic readers receive only the manifest, never archive pixels.
                if operation != "image":
                    return True, manifest
                return True, PreparedMediaData(manifest, prepared_images)
            if operation == "image":
                return True, _artifact_error("artifact_not_image", "该 Artifact 没有原生图片结果")
            if operation == "text":
                return True, sourced(
                    _read_text_artifact(
                        handle_id,
                        content,
                        offset=offset,
                        limit=limit,
                        query=query,
                        max_characters=max_characters,
                    )
                )
            if operation not in {"inspect", "get", "search"}:
                return True, _artifact_error(
                    "artifact_operation_invalid",
                    "Artifact operation 必须是 inspect、get、search、text 或 image",
                )
            try:
                decoded = json.loads(content)
                del content
            except (json.JSONDecodeError, RecursionError):
                return True, _artifact_error(
                    "artifact_not_json",
                    "Artifact 不是合法 JSON，请使用 text 模式读取",
                )
            logical_root, logical_root_name = _logical_artifact_root(decoded)
            resolved_ok, resolved = _resolve_artifact_path(logical_root, path)
            if not resolved_ok:
                assert isinstance(resolved, dict)
                return True, resolved
            base: dict[str, object] = {
                "handle": handle_id,
                "mode": "json",
                "logical_root": logical_root_name,
                "provider_id": provider_id,
                "tool_name": tool_name,
            }
            if operation == "inspect":
                return True, sourced(
                    _inspect_json(
                        resolved,
                        path=path,
                        offset=offset,
                        limit=min(limit, item_limit) if item_limit is not None else limit,
                        base=base,
                        max_characters=max_characters,
                    )
                )
            if operation == "get":
                return True, sourced(
                    _get_json(
                        resolved,
                        path=path,
                        offset=offset,
                        limit=limit
                        if isinstance(resolved, str)
                        else min(limit, item_limit)
                        if item_limit is not None
                        else limit,
                        base=base,
                        max_characters=max_characters,
                    )
                )
            if not query:
                return True, _artifact_error("artifact_query_required", "search 操作必须提供 query")
            return True, sourced(
                _search_json(
                    resolved,
                    path=path,
                    query=query,
                    offset=offset,
                    limit=min(limit, item_limit) if item_limit is not None else limit,
                    base=base,
                    max_characters=max_characters,
                )
            )

        verified, page = await asyncio.to_thread(load)
        if not verified:
            return page
        if access is not None:
            # Erasure, generation changes and GC may race the bounded file I/O.
            async with self._database.read_session() as session:
                current = await session.get(ToolArtifactModel, handle_id)
                if (
                    current is None
                    or current.deleting
                    or not await self._authorized(session, current, access)
                    or (
                        _as_utc(current.expires_at) <= datetime.now(UTC)
                        and not await session.scalar(
                            select(ToolArtifactModel.handle_id).where(
                                ToolArtifactModel.handle_id == handle_id, self._protected()
                            )
                        )
                    )
                ):
                    return _artifact_error("artifact_not_authorized", "Artifact 来源已失效")
        return page

    def _orphan_candidates(self, cutoff: float) -> list[tuple[Path, tuple[int, int, int]]]:
        if self._orphan_iterator is None:
            self._orphan_iterator = self._root.glob("*")
        paths = list(islice(self._orphan_iterator, 128))
        if len(paths) < 128:
            self._orphan_iterator = None
        result = []
        for path in paths:
            name = path.name
            immutable = (
                name.endswith(".json")
                and len(name) == 37
                and all(character in "0123456789abcdef" for character in name[:-5])
            )
            if not immutable and not name.startswith(".publishing-"):
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
                if info.st_mtime < cutoff:
                    result.append((path, (info.st_ino, info.st_size, info.st_mtime_ns)))
            except OSError:
                continue
        return result

    @staticmethod
    def _remove_orphan(path: Path, identity: tuple[int, int, int]) -> bool:
        try:
            if path.is_symlink():
                return False
            info = path.stat()
            if (info.st_ino, info.st_size, info.st_mtime_ns) != identity:
                return False
            path.unlink()
            return True
        except OSError:
            return False

    async def _cleanup_orphans(self, cutoff: float) -> int:
        candidates = await asyncio.to_thread(self._orphan_candidates, cutoff)
        if not candidates:
            return 0
        # create() holds the same lock through publication and metadata commit.
        # Immutable UUID names are never reused, so an old unregistered file
        # cannot gain a new owner between this read and its physical removal.
        async with self._database.read_session() as reader:
            registered = set(
                await reader.scalars(
                    select(ToolArtifactModel.relative_path).where(
                        ToolArtifactModel.relative_path.in_(
                            tuple(path.name for path, _ in candidates)
                        )
                    )
                )
            )
        removed = 0
        for path, identity in candidates:
            if path.name not in registered:
                removed += int(await asyncio.to_thread(self._remove_orphan, path, identity))
        return removed

    async def cleanup(self) -> int:
        now = datetime.now(UTC)
        removed = 0
        async with self._storage_lock:
            removed += await self._cleanup_orphans(now.timestamp() - 86400)
            eligible = or_(
                ToolArtifactModel.deleting.is_(True),
                and_(ToolArtifactModel.expires_at <= now, ~self._protected()),
            )
            async with self._database.read_session() as reader:
                candidates = list(
                    await reader.scalars(
                        select(ToolArtifactModel.handle_id)
                        .where(eligible)
                        .order_by(ToolArtifactModel.expires_at)
                        .limit(128)
                    )
                )
            if not candidates:
                return removed
            async with self._database.immediate_session() as writer:
                rows = list(
                    (
                        await writer.execute(
                            update(ToolArtifactModel)
                            .where(ToolArtifactModel.handle_id.in_(candidates), eligible)
                            .values(deleting=True)
                            .returning(ToolArtifactModel)
                        )
                    ).scalars()
                )
            unlinked: list[str] = []
            for row in rows:
                path = (self._root / row.relative_path).resolve()
                if self._root.resolve() in path.parents:
                    try:
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    except OSError:
                        continue
                else:
                    continue
                unlinked.append(row.handle_id)
            if unlinked:
                async with self._database.immediate_session() as writer:
                    deleted = await writer.scalars(
                        delete(ToolArtifactModel)
                        .where(
                            ToolArtifactModel.handle_id.in_(unlinked),
                            ToolArtifactModel.deleting.is_(True),
                        )
                        .returning(ToolArtifactModel.handle_id)
                    )
                    removed += len(tuple(deleted))
        return removed


def _read_text_file(
    path: Path,
    byte_size: int,
    digest: str | None,
    *,
    handle_id: str,
    offset: int,
    limit: int,
    max_characters: int,
) -> tuple[bool, dict[str, object]]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    hasher = hashlib.sha256()
    total_bytes = total_characters = 0
    pieces: list[str] = []
    invalid_utf8 = False

    def collect(text: str) -> None:
        nonlocal total_characters
        start = max(0, offset - total_characters)
        end = min(len(text), offset + min(limit, max_characters) - total_characters)
        if end > start:
            pieces.append(text[start:end])
        total_characters += len(text)

    try:
        with path.open("rb") as stream:
            while total_bytes <= byte_size:
                chunk = stream.read(min(65536, byte_size + 1 - total_bytes))
                if not chunk:
                    break
                total_bytes += len(chunk)
                hasher.update(chunk)
                if not invalid_utf8:
                    try:
                        collect(decoder.decode(chunk))
                    except UnicodeDecodeError:
                        invalid_utf8 = True
        if not invalid_utf8:
            try:
                collect(decoder.decode(b"", final=True))
            except UnicodeDecodeError:
                invalid_utf8 = True
    except OSError:
        return False, _artifact_error("artifact_missing", "Artifact 正文缺失，执行回执仍然有效")
    if total_bytes != byte_size or (digest and hasher.hexdigest() != digest):
        return False, _artifact_error("artifact_corrupt", "Artifact 完整性校验失败")
    if invalid_utf8:
        return False, _artifact_error("artifact_corrupt", "Artifact 不是有效 UTF-8")
    return True, _string_page(
        "".join(pieces),
        total=total_characters,
        offset=offset,
        base={"handle": handle_id, "mode": "text"},
        max_characters=max_characters,
    )


def _string_page(
    fragment: str,
    *,
    total: int,
    offset: int,
    base: dict[str, object],
    max_characters: int,
    query_matched: bool | None = None,
) -> dict[str, object]:
    text_mode = base.get("mode") == "text"
    field = "content" if text_mode else "value"

    def page(length: int) -> dict[str, object]:
        end = offset + length
        result = {
            **base,
            "offset": offset,
            "next_offset": end if end < total else None,
            "total_characters": total,
            field: fragment[:length],
        }
        if text_mode:
            result["query_matched"] = query_matched
        else:
            result.update(offset_unit="characters", truncated=end < total)
        return result

    from yuki.capabilities.results import artifact_page_size

    reference = page(0)
    reference["next_offset"] = None
    if not text_mode:
        reference["truncated"] = False
    fixed = cast(int, artifact_page_size(reference)) - 4 - (0 if text_mode else 5)
    used = best = 0
    terminal = offset + len(fragment) == total
    for length, character in enumerate(fragment, 1):
        used += escaped_character_length(character)
        end = offset + length
        metadata = (len(str(end)) if end < total else 4) + (
            0 if text_mode else (4 if end < total else 5)
        )
        if fixed + used + metadata <= max_characters:
            best = length
        integer_metadata = len(str(end)) + (0 if text_mode else 4)
        floor = min(integer_metadata, 4 + (0 if text_mode else 5)) if terminal else integer_metadata
        if fixed + used + floor > max_characters:
            break
    result = page(best)
    if not _fits_json_budget(result, max_characters) or (best == 0 and offset < total):
        if text_mode:
            return _artifact_error(
                "artifact_budget_too_small",
                "预算不足以返回一个字符",
                handle=base["handle"],
                offset=offset,
                next_offset=offset,
            )
        return _artifact_error(
            "artifact_budget_too_small",
            "预算不足以返回一个字符",
            **{key: value for key, value in base.items() if key != "type"},
            offset=offset,
            next_offset=offset,
        )
    return result


def _read_text_artifact(
    handle_id: str,
    content: str,
    *,
    offset: int,
    limit: int,
    query: str,
    max_characters: int = 8000,
) -> dict[str, object]:
    start = offset
    if query:
        suffix = content[offset:]
        found = suffix.casefold().find(query.casefold())
        if found < 0:
            return {
                "handle": handle_id,
                "mode": "text",
                "offset": offset,
                "next_offset": None,
                "total_characters": len(content),
                "content": "",
                "query_matched": False,
            }
        folded_offset = 0
        for index, character in enumerate(suffix, offset):
            folded_offset += len(character.casefold())
            if folded_offset > found:
                start = index
                break
    return _string_page(
        content[start : start + min(limit, max_characters)],
        total=len(content),
        offset=start,
        base={"handle": handle_id, "mode": "text"},
        max_characters=max_characters,
        query_matched=True if query else None,
    )


def _logical_artifact_root(value: object) -> tuple[object, str]:
    if (
        isinstance(value, dict)
        and "data" in value
        and ("provider_id" in value or "tool_name" in value or "ok" in value)
    ):
        return value["data"], "data"
    return value, "$"


def _resolve_artifact_path(
    root: object,
    path: tuple[str | int, ...],
) -> tuple[bool, object]:
    value = root
    traversed: list[str | int] = []
    for part in path:
        if isinstance(value, dict):
            if not isinstance(part, str) or part not in value:
                return (
                    False,
                    _artifact_error(
                        "artifact_path_not_found",
                        "Artifact 对象路径不存在",
                        path=traversed,
                        failed_part=part,
                    ),
                )
            value = value[part]
        elif isinstance(value, list):
            if isinstance(part, bool) or not isinstance(part, int) or not 0 <= part < len(value):
                return (
                    False,
                    _artifact_error(
                        "artifact_path_not_found",
                        "Artifact 数组下标不存在",
                        path=traversed,
                        failed_part=part,
                    ),
                )
            value = value[part]
        else:
            return (
                False,
                _artifact_error(
                    "artifact_path_not_found",
                    "Artifact 路径穿过了标量值",
                    path=traversed,
                    failed_part=part,
                ),
            )
        traversed.append(part)
    return True, value


def _page_fixed_size(page: dict[str, object]) -> int:
    from yuki.capabilities.results import artifact_page_size

    # The reference page contains an empty collection and a null cursor.
    return cast(int, artifact_page_size(page)) - 4


def _prefix_count(
    sizes: Iterable[int | None],
    *,
    fixed: int,
    offset: int,
    total: int,
    limit: int,
    budget: int,
) -> int:
    best = used = 0
    terminal = offset + min(limit, max(0, total - offset)) == total
    for count, size in enumerate(islice(sizes, limit), 1):
        if size is None:
            break
        used += size + (2 if count > 1 else 0)
        end = offset + count
        cursor_size = len(str(end)) if end < total else 4
        if fixed + used + cursor_size <= budget:
            best = count
        # Future integer cursors cannot shrink. Only the actual EOF can use null.
        floor = min(len(str(end)), 4) if terminal else len(str(end))
        if fixed + used + floor > budget:
            break
    return best


def _inspect_json(
    value: object,
    *,
    path: tuple[str | int, ...],
    offset: int,
    limit: int,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    result: dict[str, object] = {**base, "path": list(path), "type": _json_type(value)}
    if isinstance(value, (dict, list)):
        total = len(value)
        result.update(
            {
                "total_children" if isinstance(value, dict) else "length": total,
                "children": [],
                "next_offset": None,
            }
        )
        if offset >= total:
            return result
        if isinstance(value, dict):
            selected = nsmallest(offset + limit, value, key=lambda key: str(key).casefold())[
                offset:
            ]
            children = ({"key": str(key), **_value_shape(value[key])} for key in selected)
        else:
            children = (
                {"index": index, **_value_shape(value[index])}
                for index in range(offset, min(total, offset + limit))
            )
        prepared = []

        def sizes() -> Iterator[int | None]:
            for child in children:
                prepared.append(child)
                yield json_character_count(child, limit=max_characters)

        count = _prefix_count(
            sizes(),
            fixed=_page_fixed_size(result),
            offset=offset,
            total=total,
            limit=limit,
            budget=max_characters,
        )
        if not count:
            return _artifact_error(
                "artifact_budget_too_small",
                "无法放入一个子项，请增大预算",
                **base,
                path=list(path),
                offset=offset,
                next_offset=offset,
            )
        result.update(
            children=prepared[:count],
            next_offset=offset + count if offset + count < total else None,
        )
    elif isinstance(value, str):
        result["characters"] = len(value)
    else:
        result["value"] = value
    return result


def _get_json(
    value: object,
    *,
    path: tuple[str | int, ...],
    offset: int,
    limit: int,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    if isinstance(value, str):
        return _string_page(
            value[offset : offset + min(limit, max_characters)],
            total=len(value),
            offset=offset,
            base={**base, "path": list(path), "type": "string"},
            max_characters=max_characters,
        )
    direct = {**base, "path": list(path), "type": _json_type(value), "value": value}
    if (
        offset == 0
        and (not isinstance(value, (dict, list)) or len(value) <= limit)
        and _fits_json_budget(direct, max_characters)
    ):
        return direct
    if not isinstance(value, (dict, list)):
        return _oversized_value(path, value, base=base, max_characters=max_characters)
    total = len(value)
    result = {
        **base,
        "path": list(path),
        "type": _json_type(value),
        "total_items": total,
        "offset": offset,
        "value": {} if isinstance(value, dict) else [],
        "next_offset": None,
    }
    if offset >= total:
        return result
    cache: dict[int, tuple[object, int, bool]] = {}
    if isinstance(value, dict):
        selected = nsmallest(offset + limit, value, key=lambda key: str(key).casefold())[offset:]

        def members() -> Iterator[int | None]:
            for key in selected:
                key_size = json_character_count(str(key), limit=max_characters)
                value_size = json_character_count(value[key], limit=max_characters, cache=cache)
                yield (
                    key_size + 2 + value_size
                    if key_size is not None and value_size is not None
                    else None
                )

        sizes = members()
    else:
        sizes = (
            json_character_count(value[index], limit=max_characters, cache=cache)
            for index in range(offset, min(total, offset + limit))
        )
    count = _prefix_count(
        sizes,
        fixed=_page_fixed_size(result),
        offset=offset,
        total=total,
        limit=limit,
        budget=max_characters,
    )
    if not count:
        return _oversized_value(path, value, base=base, max_characters=max_characters)
    result.update(
        value={str(key): value[key] for key in selected[:count]}
        if isinstance(value, dict)
        else value[offset : offset + count],
        next_offset=offset + count if offset + count < total else None,
    )
    return result


def _search_json(
    value: object,
    *,
    path: tuple[str | int, ...],
    query: str,
    offset: int,
    limit: int,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    folded = query.casefold()
    matches: dict[tuple[str | int, ...], dict[str, object]] = {}
    scanned_nodes = 0
    current_path = list(path)

    def add_match(record: object, matched_key: str | None = None) -> None:
        record_path = tuple(current_path)
        if record_path not in matches:
            matches[record_path] = {
                "path": list(current_path),
                "matched_path": [*current_path, matched_key]
                if matched_key is not None
                else list(current_path),
                "value": record,
            }

    def walk(item: object, key_match: bool = False) -> None:
        nonlocal scanned_nodes
        scanned_nodes += 1
        recorded = key_match
        if key_match:
            add_match(item)
        if isinstance(item, dict):
            for key in sorted(item, key=lambda candidate: str(candidate).casefold()):
                child = item[key]
                matched = folded in str(key).casefold()
                if _is_json_scalar(child):
                    if not recorded and (matched or folded in _scalar_text(child).casefold()):
                        add_match(item, str(key))
                        recorded = True
                else:
                    current_path.append(str(key))
                    walk(child, matched)
                    current_path.pop()
        elif isinstance(item, list):
            for index, child in enumerate(item):
                current_path.append(index)
                if _is_json_scalar(child):
                    if folded in _scalar_text(child).casefold():
                        add_match(child)
                else:
                    walk(child)
                current_path.pop()
        elif not recorded and folded in _scalar_text(item).casefold():
            add_match(item)

    walk(value)
    selected = nsmallest(offset + limit, matches, key=_path_sort_key)[offset:]
    result = {
        **base,
        "query": query,
        "matches": [],
        "next_offset": None,
        "scanned_nodes": scanned_nodes,
    }
    prepared = []
    cache: dict[int, tuple[object, int, bool]] = {}

    def sizes() -> Iterator[int | None]:
        from yuki.capabilities.results import artifact_page_size

        for key in selected:
            candidate = dict(matches[key])
            value_size = json_character_count(candidate["value"], limit=max_characters, cache=cache)
            fixed = cast(int, artifact_page_size({**candidate, "value": None})) - 4
            if value_size is None or fixed + value_size > max_characters:
                record = candidate.pop("value")
                candidate.update(
                    value_omitted=True,
                    value_shape=_value_shape(record),
                    instruction="匹配对象过大，请对 path 执行 inspect 或 get",
                )
            prepared.append(candidate)
            yield json_character_count(candidate, limit=max_characters, cache=cache)

    count = _prefix_count(
        sizes(),
        fixed=_page_fixed_size(result),
        offset=offset,
        total=len(matches),
        limit=limit,
        budget=max_characters,
    )
    if selected and not count:
        return _artifact_error(
            "artifact_budget_too_small",
            "无法放入一个匹配，请增大预算或读取更深path",
            **base,
            path=list(path),
            offset=offset,
            next_offset=offset,
        )
    result.update(
        matches=prepared[:count],
        next_offset=offset + count if offset + count < len(matches) else None,
    )
    return result


def _oversized_value(
    path: tuple[str | int, ...],
    value: object,
    *,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    result = _artifact_error(
        "artifact_value_too_large",
        "目标值无法完整放入当前工具结果预算，请读取更深层路径",
        **base,
        path=list(path),
        value_shape=_value_shape(value),
        estimated_characters=0,
    )
    if not _fits_json_budget(result, max_characters):
        result.pop("estimated_characters", None)
        return result
    result["estimated_characters"] = _json_size(value)
    if not _fits_json_budget(result, max_characters):
        result.pop("estimated_characters", None)
    return result


def _artifact_error(error_code: str, detail: str, **metadata: object) -> dict[str, object]:
    return {"error_code": error_code, "detail": detail, **metadata}


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _value_shape(value: object) -> dict[str, object]:
    shape: dict[str, object] = {"type": _json_type(value)}
    if isinstance(value, dict):
        shape["child_count"] = len(value)
    elif isinstance(value, list):
        shape["length"] = len(value)
    elif isinstance(value, str):
        shape["characters"] = len(value)
    return shape


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _scalar_text(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _path_sort_key(path: tuple[str | int, ...]) -> tuple[str, ...]:
    return tuple(f"{type(part).__name__}:{part}" for part in path)


def _fits_json_budget(value: object, max_characters: int) -> bool:
    from yuki.capabilities.results import artifact_page_fits

    return artifact_page_fits(value, max_characters)


def _json_size(value: object, *, compact: bool = False) -> int | None:
    return json_character_count(value, compact=compact)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
