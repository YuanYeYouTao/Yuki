"""Application-owned tool result artifacts and retained research material."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.capabilities.media import PreparedMediaData
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ToolArtifactModel,
)
from qq_ai_bot.tool_results.access import ArtifactAccess

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
        from qq_ai_bot.runtime.work_schema_v1 import work
        from qq_ai_bot.runtime.work_tree import ancestors
        from qq_ai_bot.tool_results.schema import artifact_refs

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

        from qq_ai_bot.tool_results.schema import artifact_refs

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
    async def release_refs(session: AsyncSession, owner_kind: str, owner_id: str) -> None:
        from qq_ai_bot.tool_results.schema import artifact_refs

        await session.execute(
            delete(artifact_refs).where(
                artifact_refs.c.owner_kind == owner_kind, artifact_refs.c.owner_id == owner_id
            )
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
            return all(
                source.get(key) == value
                for key, value in (
                    ("conversation_id", access.conversation_id),
                    ("generation", access.generation),
                    ("actor_person_id", access.actor_person_id),
                    ("principal_kind", access.principal_kind),
                    ("read_scope", access.read_scope),
                )
            )
        # A legacy handle is not a public bearer capability. Its original Work
        # remains the trusted source; unowned old results cannot grant access.
        if row.work_id is None:
            return False
        from qq_ai_bot.runtime.work_schema_v1 import work

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
        return bool(
            source.get("actor_person_id") == access.actor_person_id
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
        content: str,
        media_type: str,
        retention_seconds: int | None = None,
        work_id: str | None = None,
        effect_key: str | None = None,
        access: ArtifactAccess | None = None,
    ) -> str:
        from qq_ai_bot.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None and capture.work_id and capture.effect_key and work_id is None:
            work_id, effect_key = capture.work_id, capture.effect_key
        handle = uuid.uuid4().hex
        relative = f"{handle}.json"
        path = self._root / relative
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if len(encoded) > self._max_artifact_bytes:
            raise ValueError("tool_artifact_capacity")
        retention = retention_seconds if retention_seconds is not None else self._retention
        if retention <= 0:
            raise ValueError("artifact retention must be positive")
        async with self._database.sessions() as reader:
            privacy = int(
                await reader.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
        access_json = access.encode(privacy) if access is not None else None
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        async with self._storage_lock:
            async with self._database.sessions() as reader:
                used = await reader.scalar(
                    select(func.coalesce(func.sum(ToolArtifactModel.byte_size), 0))
                )
            if int(used or 0) + len(encoded) > self._max_total_bytes:
                raise ValueError("tool_artifact_capacity")
            await asyncio.to_thread(self._publish, path, encoded)
            now = datetime.now(UTC)
            # Cancellation can arrive after COMMIT: bounded orphan maintenance
            # owns unregistered files; never unlink a potentially durable ref here.
            async with self._database.immediate_session() as session:
                if access is not None:
                    if (
                        not await session.scalar(
                            select(CanonicalConversationModel.id).where(
                                CanonicalConversationModel.id == access.conversation_id,
                                CanonicalConversationModel.generation == access.generation,
                            )
                        )
                        or int(
                            await session.scalar(
                                select(ExecutionTraceStateModel.privacy_generation).where(
                                    ExecutionTraceStateModel.id == 1
                                )
                            )
                            or 0
                        )
                        != privacy
                    ):
                        raise ValueError("artifact_source_changed")
                if work_id is not None:
                    from qq_ai_bot.runtime.work_schema_v1 import work

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
        if len(content.encode()) > self._max_media_bytes + 65536:
            raise ValueError("artifact_media_capacity")
        return await self.write_artifact(
            provider_id=provider_id,
            tool_name=tool_name,
            content=content,
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
        async with self._database.sessions() as session:
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
        if media_type == _PRIVATE_MEDIA_TYPE and access is None:
            return _artifact_error("artifact_not_authorized", "图片 Artifact 需要原读取授权")
        file_path = (self._root / relative).resolve()
        root = self._root.resolve()
        if root not in file_path.parents:
            return None
        try:
            raw = await asyncio.to_thread(self._read_bounded, file_path, byte_size)
            if len(raw) != byte_size or (digest and hashlib.sha256(raw).hexdigest() != digest):
                return _artifact_error("artifact_corrupt", "Artifact 完整性校验失败")
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _artifact_error("artifact_corrupt", "Artifact 不是有效 UTF-8")
        except OSError:
            return _artifact_error("artifact_missing", "Artifact 正文缺失，执行回执仍然有效")
        if media_type == _PRIVATE_MEDIA_TYPE:
            try:
                decoded_media = json.loads(content)
                prepared_images = tuple(
                    replace(ChatImage(**item), source="tool", tool_handle=handle_id)
                    for item in decoded_media["images"]
                )
                if not prepared_images or len(prepared_images) > self._max_media_frames:
                    raise ValueError("invalid media count")
            except (ValueError, TypeError, KeyError, RecursionError):
                return _artifact_error("artifact_corrupt", "图片 Artifact 完整性校验失败")
            manifest = {
                "handle": handle_id,
                "mode": "image",
                "image_count": len(prepared_images),
                "available_operations": ["inspect", "image"],
                "provider_id": provider_id,
                "tool_name": tool_name,
            }
        if access is not None:
            # Erasure, generation changes and GC may race the bounded file I/O.
            async with self._database.sessions() as session:
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
        if media_type == _PRIVATE_MEDIA_TYPE:
            # Generic readers receive only the manifest, never archive pixels.
            if operation != "image":
                return manifest
            return PreparedMediaData(manifest, prepared_images)
        if operation == "image":
            return _artifact_error("artifact_not_image", "该 Artifact 没有原生图片结果")
        if operation == "text":
            return _read_text_artifact(
                handle_id,
                content,
                offset=offset,
                limit=limit,
                query=query,
                max_characters=max_characters,
            )
        if operation not in {"inspect", "get", "search"}:
            return _artifact_error(
                "artifact_operation_invalid",
                "Artifact operation 必须是 inspect、get、search、text 或 image",
            )
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, RecursionError):
            return _artifact_error(
                "artifact_not_json",
                "Artifact 不是合法 JSON，请使用 text 模式读取",
            )
        logical_root, logical_root_name = _logical_artifact_root(decoded)
        resolved_ok, resolved = _resolve_artifact_path(logical_root, path)
        if not resolved_ok:
            assert isinstance(resolved, dict)
            return resolved
        base: dict[str, object] = {
            "handle": handle_id,
            "mode": "json",
            "logical_root": logical_root_name,
            "provider_id": provider_id,
            "tool_name": tool_name,
        }
        if operation == "inspect":
            return _inspect_json(
                resolved,
                path=path,
                offset=offset,
                limit=min(limit, item_limit) if item_limit is not None else limit,
                base=base,
                max_characters=max_characters,
            )
        if operation == "get":
            return _get_json(
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
        if not query:
            return _artifact_error("artifact_query_required", "search 操作必须提供 query")
        return _search_json(
            resolved,
            path=path,
            query=query,
            offset=offset,
            limit=min(limit, item_limit) if item_limit is not None else limit,
            base=base,
            max_characters=max_characters,
        )

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
        async with self._database.sessions() as reader:
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
            async with self._database.sessions() as reader:
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
        found = content.casefold().find(query.casefold(), offset)
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
        start = found

    def page(length: int) -> dict[str, object]:
        end = min(len(content), start + length)
        return {
            "handle": handle_id,
            "mode": "text",
            "offset": start,
            "next_offset": end if end < len(content) else None,
            "total_characters": len(content),
            "content": content[start:end],
            "query_matched": True if query else None,
        }

    low, high = 0, min(limit, max(0, len(content) - start))
    while low < high:
        middle = (low + high + 1) // 2
        if _fits_json_budget(page(middle), max_characters):
            low = middle
        else:
            high = middle - 1
    if not _fits_json_budget(page(low), max_characters) or (low == 0 and start < len(content)):
        return {
            "error_code": "artifact_budget_too_small",
            "handle": handle_id,
            "offset": start,
            "next_offset": start,
            "detail": "预算不足以返回一个字符",
        }
    return page(low)


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
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: str(item).casefold())
        selected = keys[offset : offset + limit]
        while selected:
            candidate = {
                **result,
                "total_children": len(keys),
                "children": [{"key": str(key), **_value_shape(value[key])} for key in selected],
                "next_offset": (
                    offset + len(selected) if offset + len(selected) < len(keys) else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected.pop()
        if offset < len(keys):
            return _artifact_error(
                "artifact_budget_too_small",
                "无法放入一个子项，请增大预算",
                **base,
                path=list(path),
                offset=offset,
                next_offset=offset,
            )
        result.update({"total_children": len(keys), "children": [], "next_offset": None})
    elif isinstance(value, list):
        selected = value[offset : offset + limit]
        while selected:
            candidate = {
                **result,
                "length": len(value),
                "children": [
                    {"index": offset + index, **_value_shape(item)}
                    for index, item in enumerate(selected)
                ],
                "next_offset": (
                    offset + len(selected) if offset + len(selected) < len(value) else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected = selected[:-1]
        if offset < len(value):
            return _artifact_error(
                "artifact_budget_too_small",
                "无法放入一个子项，请增大预算",
                **base,
                path=list(path),
                offset=offset,
                next_offset=offset,
            )
        result.update({"length": len(value), "children": [], "next_offset": None})
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
        # Strings page by Unicode characters; workspace cursors remain bytes.
        # Fit the encoded page, including escaping and its metadata, not a preview.
        def string_page(length: int) -> dict[str, object]:
            end = offset + length
            return {
                **base,
                "path": list(path),
                "type": "string",
                "value": value[offset:end],
                "offset": offset,
                "offset_unit": "characters",
                "next_offset": end if end < len(value) else None,
                "total_characters": len(value),
                "truncated": end < len(value),
            }

        low, high = 0, min(limit, max(0, len(value) - offset))
        while low < high:
            middle = (low + high + 1) // 2
            if _fits_json_budget(string_page(middle), max_characters):
                low = middle
            else:
                high = middle - 1
        if not _fits_json_budget(string_page(low), max_characters) or (
            low == 0 and offset < len(value)
        ):
            return _artifact_error(
                "artifact_budget_too_small",
                "预算不足以返回一个字符",
                **base,
                path=list(path),
                offset=offset,
                next_offset=offset,
            )
        return string_page(low)
    direct = {**base, "path": list(path), "type": _json_type(value), "value": value}
    if (
        offset == 0
        and (not isinstance(value, (dict, list)) or len(value) <= limit)
        and _fits_json_budget(direct, max_characters)
    ):
        return direct
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: str(item).casefold())
        selected = keys[offset : offset + limit]
        if not selected and offset >= len(keys):
            return {
                **base,
                "path": list(path),
                "type": "object",
                "total_items": len(keys),
                "offset": offset,
                "value": {},
                "next_offset": None,
            }
        while selected:
            page = {str(key): value[key] for key in selected}
            candidate = {
                **base,
                "path": list(path),
                "type": "object",
                "total_items": len(keys),
                "offset": offset,
                "value": page,
                "next_offset": (
                    offset + len(selected) if offset + len(selected) < len(keys) else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected.pop()
        return _oversized_value(path, value, base=base, max_characters=max_characters)
    if isinstance(value, list):
        selected_values = value[offset : offset + limit]
        if not selected_values and offset >= len(value):
            return {
                **base,
                "path": list(path),
                "type": "array",
                "total_items": len(value),
                "offset": offset,
                "value": [],
                "next_offset": None,
            }
        while selected_values:
            candidate = {
                **base,
                "path": list(path),
                "type": "array",
                "total_items": len(value),
                "offset": offset,
                "value": selected_values,
                "next_offset": (
                    offset + len(selected_values)
                    if offset + len(selected_values) < len(value)
                    else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected_values = selected_values[:-1]
        return _oversized_value(path, value, base=base, max_characters=max_characters)
    return _oversized_value(path, value, base=base, max_characters=max_characters)


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

    def add_match(
        record_path: tuple[str | int, ...],
        matched_path: tuple[str | int, ...],
        record: object,
    ) -> None:
        matches.setdefault(
            record_path,
            {
                "path": list(record_path),
                "matched_path": list(matched_path),
                "value": record,
            },
        )

    def walk(item: object, item_path: tuple[str | int, ...]) -> None:
        nonlocal scanned_nodes
        scanned_nodes += 1
        if isinstance(item, dict):
            for key in sorted(item, key=lambda candidate: str(candidate).casefold()):
                child = item[key]
                child_path = (*item_path, str(key))
                if folded in str(key).casefold():
                    if isinstance(child, (dict, list)):
                        add_match(child_path, child_path, child)
                    else:
                        add_match(item_path, child_path, item)
                if _is_json_scalar(child):
                    if folded in _scalar_text(child).casefold():
                        add_match(item_path, child_path, item)
                else:
                    walk(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                child_path = (*item_path, index)
                if _is_json_scalar(child):
                    if folded in _scalar_text(child).casefold():
                        add_match(child_path, child_path, child)
                else:
                    walk(child, child_path)
        elif folded in _scalar_text(item).casefold():
            add_match(item_path, item_path, item)

    walk(value, path)
    ordered = [matches[key] for key in sorted(matches, key=_path_sort_key)]
    selected = ordered[offset : offset + limit]
    rendered: list[dict[str, object]] = []
    for match in selected:
        candidate = dict(match)
        if not _fits_json_budget(candidate, max_characters):
            record = candidate.pop("value")
            candidate.update(
                {
                    "value_omitted": True,
                    "value_shape": _value_shape(record),
                    "instruction": "匹配对象过大，请对 path 执行 inspect 或 get",
                }
            )
        aggregate = {
            **base,
            "query": query,
            "matches": [*rendered, candidate],
            "next_offset": None,
            "scanned_nodes": scanned_nodes,
        }
        if not _fits_json_budget(aggregate, max_characters):
            break
        rendered.append(candidate)
    if selected and not rendered:
        return _artifact_error(
            "artifact_budget_too_small",
            "无法放入一个匹配，请增大预算或读取更深path",
            **base,
            path=list(path),
            offset=offset,
            next_offset=offset,
        )
    has_more = offset + len(rendered) < len(ordered) or len(rendered) < len(selected)
    return {
        **base,
        "query": query,
        "matches": rendered,
        "next_offset": offset + len(rendered) if has_more else None,
        "scanned_nodes": scanned_nodes,
    }


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
        estimated_characters=_json_size(value),
    )
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
    from qq_ai_bot.capabilities.results import artifact_page_fits

    return artifact_page_fits(value, max_characters)


def _json_size(value: object, *, compact: bool = False) -> int | None:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":") if compact else None,
        )
    except (RecursionError, ValueError):
        return None
    return len(rendered)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
