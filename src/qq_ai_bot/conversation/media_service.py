"""Conversation-bound, fixed-lifetime media bytes and on-demand inspection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text, tuple_, update

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.capabilities.media import PreparedMediaData
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.gateway.media import MediaKind, PlatformMediaFile, PlatformMediaGateway
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from qq_ai_bot.services.media_process import MediaProcessError
from qq_ai_bot.services.media_process import run_media_process as run_parser
from qq_ai_bot.services.media_resolver import (
    MediaResolutionError,
    MediaResolver,
)
from qq_ai_bot.services.native_media import MAX_IMAGE_BYTES, NativeMediaPreparer
from qq_ai_bot.services.vision_service import VisionProcessingError
from qq_ai_bot.vision.models import MediaReference

_LIFETIME = timedelta(hours=24)
_MAX_FILE = 200 * 1024 * 1024
_GLOBAL_BUDGET = 4 * 1024 * 1024 * 1024
_CONVERSATION_BUDGET = 512 * 1024 * 1024
_CLEANUP_PAGE = 128
_GC_PATH_BYTES = 64 * 1024
_GC_SECONDS = 0.1


async def _gc_io[GCResult](call: Callable[..., GCResult], *args: Any) -> GCResult:
    # Cancellation cannot stop a running filesystem syscall. Finish this
    # bounded page before releasing the publication/iterator locks.
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    if cancelled is not None:
        raise cancelled
    return task.result()


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    path: Path
    token: tuple[int, int, int, int, int]


def _file_token(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _cache_entries(root: Path, depth: int = 3) -> Iterator[Path]:
    """Stream the fixed cache layout; never follow directory symlinks."""
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                path = Path(entry.path)
                if depth > 1 and entry.is_dir(follow_symlinks=False):
                    yield from _cache_entries(path, depth - 1)
                yield path
    except OSError:
        return


def _gc_page(iterator: Iterator[Path]) -> tuple[tuple[_CacheEntry, ...], bool]:
    """Limit metadata/path memory and check elapsed time between filesystem calls."""
    deadline = time.monotonic() + _GC_SECONDS
    paths: list[_CacheEntry] = []
    path_bytes = 0
    for _ in range(_CLEANUP_PAGE):
        if time.monotonic() >= deadline or path_bytes >= _GC_PATH_BYTES:
            break
        try:
            path = next(iterator)
        except StopIteration:
            return tuple(paths), True
        path_bytes += len(os.fsencode(path))
        try:
            paths.append(_CacheEntry(path, _file_token(path.lstat())))
        except OSError:
            continue
    return tuple(paths), False


def _remove_cache_entries(entries: tuple[_CacheEntry, ...]) -> tuple[_CacheEntry, ...]:
    deadline = time.monotonic() + _GC_SECONDS
    for index, entry in enumerate(entries):
        if time.monotonic() >= deadline:
            return entries[index:]
        try:
            info = entry.path.lstat()
            if _file_token(info) != entry.token:
                continue
            if stat.S_ISDIR(info.st_mode):
                entry.path.rmdir()
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                entry.path.unlink(missing_ok=True)
        except OSError:
            continue
    return ()


class ConversationMediaError(RuntimeError):
    """Public category without a raw gateway URL or host path."""


@dataclass(frozen=True)
class _EventMediaGateway:
    database: Database
    registry: GatewayConnectionRegistry | None
    event_id: int

    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int
    ) -> PlatformMediaFile:
        async with self.database.sessions() as session:
            presence_id = await session.scalar(
                select(ChatEventModel.ingress_presence_id).where(ChatEventModel.id == self.event_id)
            )
        if presence_id is None or self.registry is None:
            raise MediaResolutionError("resource_unavailable", "附件原接收连接不可用")
        try:
            connection = self.registry.resolve_active(presence_id)
        except RegistryClosed as exc:
            raise MediaResolutionError("resource_unavailable", "附件原接收连接不可用") from exc
        gateway = self.registry.media(connection)
        if gateway is None:
            raise MediaResolutionError("resource_unavailable", "附件缺少原生下载接口")
        return await gateway.resolve_media_file(file, kind, max_download_bytes=max_download_bytes)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _CacheFileFacts:
    head: bytes
    digest: str
    byte_size: int
    token: tuple[int, int, int, int, int]


def _cache_file_facts(path: Path, max_bytes: int) -> _CacheFileFacts:
    """Hash downloaded bytes with a fixed read budget, before opening a writer."""
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= max_bytes:
        raise ConversationMediaError("attachment_invalid")
    token = _file_token(initial)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if _file_token(os.fstat(stream.fileno())) != token:
            raise ConversationMediaError("attachment_changed")
        digest = hashlib.sha256()
        head = b""
        byte_size = 0
        while chunk := stream.read(64 * 1024):
            byte_size += len(chunk)
            if byte_size > max_bytes:
                raise ConversationMediaError("attachment_invalid")
            if len(head) < 12:
                head += chunk[: 12 - len(head)]
            digest.update(chunk)
        if (
            byte_size != initial.st_size
            or _file_token(os.fstat(stream.fileno())) != token
            or _file_token(path.lstat()) != token
        ):
            raise ConversationMediaError("attachment_changed")
    return _CacheFileFacts(head, digest.hexdigest(), byte_size, token)


def _publish_cache_file(temporary: Path, final: Path, facts: _CacheFileFacts) -> None:
    if _file_token(temporary.lstat()) != facts.token:
        raise ConversationMediaError("attachment_changed")
    os.replace(temporary, final)


class ConversationMediaService:
    def __init__(
        self,
        database: Database,
        root: Path,
        resolver: MediaResolver,
        preprocessor: ImagePreprocessor,
        *,
        gateway_registry: GatewayConnectionRegistry | None = None,
        max_prepared_bytes: int = 6_291_456,
    ) -> None:
        self.database = database
        self.root = root / "v1"
        self.resolver = resolver
        self.preprocessor = preprocessor
        self.gateway_registry = gateway_registry
        self.preparer = NativeMediaPreparer(preprocessor, max_bytes=max_prepared_bytes)
        self._lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._gc_iterator: Iterator[Path] | None = None
        self._gc_pending: tuple[_CacheEntry, ...] = ()
        self._prefetch_slots = asyncio.Semaphore(2)
        self._pending: set[asyncio.Task[None]] = set()
        self._cleaner: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self.cleanup()
        self._cleaner = asyncio.create_task(self._cleanup_loop(), name="conversation-media-cleanup")

    async def close(self) -> None:
        if self._cleaner is not None:
            self._cleaner.cancel()
        for task in tuple(self._pending):
            task.cancel()
        await asyncio.gather(
            *(tuple(self._pending) + ((self._cleaner,) if self._cleaner else ())),
            return_exceptions=True,
        )
        self._cleaner = None
        async with self._cleanup_lock:
            if self._gc_iterator is not None:
                closer = getattr(self._gc_iterator, "close", None)
                if closer is not None:
                    await _gc_io(closer)
                self._gc_iterator = None
            self._gc_pending = ()

    def submit(self, event_id: int, gateway: PlatformMediaGateway | None) -> None:
        if len(self._pending) >= 32:
            return
        task = asyncio.create_task(
            self._cache_event(event_id, gateway), name=f"conversation-media-{event_id}"
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _cache_event(self, event_id: int, gateway: PlatformMediaGateway | None) -> None:
        async with self.database.sessions() as session:
            items = (
                await session.scalars(
                    select(ConversationMediaItemModel).where(
                        ConversationMediaItemModel.source_event_id == event_id
                    )
                )
            ).all()
        for item in items:
            try:
                async with self._prefetch_slots:
                    await self._cache(item.source_event_id, item.attachment_index, gateway)
            except (ConversationMediaError, MediaResolutionError, OSError, ValueError):
                # Failed prefetch remains uncached and can be retried on inspection.
                continue

    async def _sources(
        self, keys: tuple[tuple[int, int], ...]
    ) -> dict[
        tuple[int, int],
        tuple[ConversationMediaItemModel, int | None, int | None, dict[str, Any], str],
    ]:
        """Prepare selected metadata together, parsing each source event once."""
        if not keys:
            return {}
        values = func.json_each(json.dumps(keys)).table_valued("value")
        selected = select(
            func.json_extract(values.c.value, "$[0]"),
            func.json_extract(values.c.value, "$[1]"),
        )
        model = ConversationMediaItemModel
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            rows = (
                await session.execute(
                    select(
                        model,
                        CanonicalConversationModel.generation,
                        CanonicalConversationModel.starts_after_event_id,
                    )
                    .join(ChatEventModel, ChatEventModel.id == model.source_event_id)
                    .outerjoin(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == model.conversation_id,
                    )
                    .where(
                        tuple_(model.source_event_id, model.attachment_index).in_(selected),
                        ChatEventModel.suppression_status == "keeper",
                        ChatEventModel.direction == "inbound",
                        ChatEventModel.canonical_conversation_id == model.conversation_id,
                    )
                )
            ).all()
            metadata = {(row[0].source_event_id, row[0].attachment_index): row for row in rows}
            event_ids = tuple(dict.fromkeys(key[0] for key in metadata))
            selected_events = func.json_each(json.dumps(event_ids)).table_valued("value")
            raw_segments = {
                event_identity: segments
                for event_identity, segments in (
                    await session.execute(
                        select(ChatEventModel.id, ChatEventModel.segments_json).where(
                            ChatEventModel.id.in_(select(selected_events.c.value))
                        )
                    )
                ).all()
            }
        parsed: dict[int, Any] = {}
        sources: dict[
            tuple[int, int],
            tuple[ConversationMediaItemModel, int | None, int | None, dict[str, Any], str],
        ] = {}
        for key in keys:
            row = metadata.get(key)
            if row is None:
                raise ConversationMediaError("attachment_not_found")
            item, generation, floor = row
            try:
                if key[0] not in parsed:
                    parsed[key[0]] = json.loads(raw_segments[key[0]])
                segment = parsed[key[0]][item.segment_index]
            except (ValueError, TypeError, IndexError) as exc:
                raise ConversationMediaError("attachment_source_invalid") from exc
            if (
                not isinstance(segment, dict)
                or segment.get("type") != item.kind
                or not isinstance(segment.get("data"), dict)
            ):
                raise ConversationMediaError("attachment_source_invalid")
            sources[key] = (item, generation, floor, segment["data"], raw_segments[key[0]])
        return sources

    def _path(self, item: ConversationMediaItemModel) -> Path:
        if not item.cache_name or "/" in item.cache_name or "\\" in item.cache_name:
            raise ConversationMediaError("cache_missing")
        if item.cache_name == f"upload-{item.content_sha256}.blob":
            return self.root / "uploads" / f"{item.content_sha256}.blob"
        return self.root / item.conversation_id / str(item.source_event_id) / item.cache_name

    async def _cache(
        self,
        event_id: int,
        index: int,
        gateway: PlatformMediaGateway | None,
        *,
        source: tuple[ConversationMediaItemModel, int | None, int | None, dict[str, Any], str]
        | None = None,
    ) -> Path:
        if source is None:
            source = (await self._sources(((event_id, index),)))[(event_id, index)]
        item, _, _, data, source_segments = source
        if item.cache_status == "expired":
            raise ConversationMediaError("attachment_expired")
        if item.expires_at is not None:
            if _aware(item.expires_at) <= datetime.now(UTC):
                await self.cleanup()
                raise ConversationMediaError("attachment_expired")
            path = self._path(item)
            if path.is_file() and not path.is_symlink():
                return path
        if item.declared_size is not None and item.declared_size > _MAX_FILE:
            raise ConversationMediaError("attachment_too_large")
        directory = self.root / item.conversation_id / str(event_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = directory / f"{uuid4().hex}.part"
        reference = MediaReference(file=data.get("file"), url=data.get("url"), source="current")
        gateway = gateway or _EventMediaGateway(self.database, self.gateway_registry, event_id)
        try:
            if item.kind == "image":
                result = await self.resolver.resolve(reference, gateway)
                if len(result.content) > _MAX_FILE:
                    raise ConversationMediaError("attachment_too_large")
                await asyncio.to_thread(temporary.write_bytes, result.content)
            else:
                await self.resolver.download_attachment(
                    reference,
                    temporary,
                    max_download_bytes=_MAX_FILE,
                    gateway=gateway,
                    kind="video" if item.kind == "video" else "file",
                )
            facts = await asyncio.to_thread(_cache_file_facts, temporary, _MAX_FILE)
            if item.kind == "image":
                if facts.head.startswith(b"\xff\xd8\xff"):
                    extension = ".jpg"
                elif facts.head.startswith(b"\x89PNG"):
                    extension = ".png"
                elif facts.head.startswith(b"GIF8"):
                    extension = ".gif"
                elif facts.head.startswith(b"RIFF") and facts.head[8:12] == b"WEBP":
                    extension = ".webp"
                else:
                    raise ConversationMediaError("attachment_type_invalid")
            elif item.kind == "video":
                if facts.head[4:8] != b"ftyp":
                    raise ConversationMediaError("attachment_type_invalid")
                extension = ".mp4"
            else:
                extension = ".bin"
            digest = facts.digest
            final_name = f"{index}-{digest}{extension}"
            final = directory / final_name
            async with self._lock:
                async with self.database.sessions() as session:
                    live = await session.get(ConversationMediaItemModel, (event_id, index))
                    event = await session.get(ChatEventModel, event_id)
                    if event is None or event.segments_json != source_segments:
                        raise ConversationMediaError("attachment_source_changed")
                    if live is None or live.cache_status == "expired":
                        raise ConversationMediaError("attachment_expired")
                    if live.cache_status == "cached" and live.cache_name:
                        existing = self._path(live)
                        if existing.is_file():
                            return existing
                        if live.expires_at and _aware(live.expires_at) <= datetime.now(UTC):
                            raise ConversationMediaError("attachment_expired")

                # Capacity rejection never removes an unexpired attachment.
                def occupied_bytes(root: Path) -> int:
                    return sum(
                        p.stat().st_size
                        for p in root.rglob("*")
                        if p.is_file() and not p.name.endswith(".part")
                    )

                occupied = await asyncio.to_thread(occupied_bytes, self.root)
                local = await asyncio.to_thread(occupied_bytes, self.root / item.conversation_id)
                if (
                    occupied + facts.byte_size > _GLOBAL_BUDGET
                    or local + facts.byte_size > _CONVERSATION_BUDGET
                ):
                    raise ConversationMediaError("cache_budget_exhausted")
                await asyncio.to_thread(_publish_cache_file, temporary, final, facts)
                now = datetime.now(UTC)
                async with self.database.immediate_session() as session:
                    live = await session.get(ConversationMediaItemModel, (event_id, index))
                    event = await session.get(ChatEventModel, event_id)
                    if event is None or event.segments_json != source_segments:
                        raise ConversationMediaError("attachment_source_changed")
                    if live is None or live.cache_status == "expired":
                        raise ConversationMediaError("attachment_expired")
                    live.cache_status = "cached"
                    live.cache_name = final_name
                    live.content_sha256 = digest
                    if live.cached_at is None:
                        live.cached_at = now
                        live.expires_at = now + _LIFETIME
            return final
        except MediaResolutionError as exc:
            raise ConversationMediaError(exc.code) from exc
        finally:
            temporary.unlink(missing_ok=True)

    async def authorized_path(
        self,
        *,
        event_id: int,
        attachment_index: int,
        conversation_id: str,
        generation: int | None,
    ) -> tuple[ConversationMediaItemModel, Path]:
        key = (event_id, attachment_index)
        source = (await self._sources((key,)))[key]
        item, current_generation, floor, _, source_segments = source
        if (
            current_generation is None
            or floor is None
            or item.conversation_id != conversation_id
            or item.generation != current_generation
            or event_id <= floor
            or (generation is not None and generation != current_generation)
        ):
            raise ConversationMediaError("attachment_scope_denied")
        path = await self._cache(event_id, attachment_index, None, source=source)
        # Recheck after network work: /ai new may have advanced during download.
        async with self.database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            event = await session.get(ChatEventModel, event_id)
            if event is None or event.segments_json != source_segments:
                raise ConversationMediaError("attachment_source_changed")
            if (
                conversation is None
                or item.generation != conversation.generation
                or event_id <= conversation.starts_after_event_id
            ):
                raise ConversationMediaError("attachment_scope_denied")
        return item, path

    async def inspect(
        self,
        item: ConversationMediaItemModel,
        path: Path,
        question: str,
        *,
        runtime: VisionRuntimeConfig | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._inspect(item, path, question, runtime=runtime)
        except (VisionProcessingError, ImagePreprocessingError) as exc:
            raise ConversationMediaError(exc.code) from exc

    async def _validated_cached_path(
        self, item: ConversationMediaItemModel, path: Path
    ) -> ConversationMediaItemModel:
        key = (item.source_event_id, item.attachment_index)
        live, generation, floor, _data, _segments = (await self._sources((key,)))[key]
        if generation != item.generation or floor is None or item.source_event_id <= floor:
            raise ConversationMediaError("attachment_scope_denied")
        if (
            live.cache_status != "cached"
            or not live.content_sha256
            or live.expires_at is None
            or _aware(live.expires_at) <= datetime.now(UTC)
        ):
            raise ConversationMediaError("attachment_expired")
        if self._path(live) != path or (
            item.content_sha256 is not None and item.content_sha256 != live.content_sha256
        ):
            raise ConversationMediaError("attachment_source_changed")
        return live

    async def _inspect(
        self,
        item: ConversationMediaItemModel,
        path: Path,
        question: str,
        *,
        runtime: VisionRuntimeConfig | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(question) <= 2000:
            raise ConversationMediaError("invalid_question")
        # Cache metadata returned by authorized_path predates download. Fetch
        # the published version again, without resolving its URL a second time.
        item = await self._validated_cached_path(item, path)
        digest = item.content_sha256

        def content_kind() -> str:
            if item.kind != "file":
                return item.kind
            with path.open("rb") as stream:
                header = stream.read(12)
            if header[4:8] == b"ftyp":
                return "video"
            if header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")) or (
                header.startswith(b"RIFF") and header[8:12] == b"WEBP"
            ):
                return "image"
            return "file"

        media_kind = await asyncio.to_thread(content_kind)
        if media_kind == "file":
            suffix = Path(item.display_name).suffix.lower()
            try:
                raw = await run_parser(
                    sys.executable,
                    "-m",
                    "qq_ai_bot.services.document_reader",
                    str(path),
                    suffix,
                    "20000",
                    "20",
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                        "PYTHONPATH": os.pathsep.join(sys.path),
                        "PYTHONIOENCODING": "utf-8",
                    },
                )
            except MediaProcessError as exc:
                raise ConversationMediaError("document_reader_failed") from exc
            result = json.loads(raw)
            if result.get("error"):
                raise ConversationMediaError(str(result["error"]))
            await self._validated_cached_path(item, path)
            return {
                "event_id": item.source_event_id,
                "attachment_index": item.attachment_index,
                "sha256": digest,
                "mode": "document",
                "reader_version": "document-reader-v1",
                "result": result,
            }
        if media_kind == "video":
            limit = runtime.video_max_download_bytes if runtime else _MAX_FILE
            if (await asyncio.to_thread(path.stat)).st_size > limit:
                raise ConversationMediaError("attachment_too_large")
            images = await self.preparer.prepare_video(path, source="history", runtime=runtime)
        else:

            def read_image() -> bytes:
                with path.open("rb") as stream:
                    data = stream.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != digest:
                    raise ConversationMediaError("attachment_invalid")
                return data

            data = await asyncio.to_thread(read_image)
            images = await asyncio.to_thread(
                self.preparer.prepare_image,
                data,
                source="history",
                max_frames=runtime.max_frames_per_turn if runtime else None,
            )
        images = tuple(
            replace(
                image,
                conversation_id=item.conversation_id,
                generation=item.generation,
                source_event_id=item.source_event_id,
                attachment_index=item.attachment_index,
                content_hash=digest,
                expires_at=_aware(item.expires_at).isoformat() if item.expires_at else None,
            )
            for image in images
        )
        await self.validate_images(images)
        return PreparedMediaData(
            {
                "event_id": item.source_event_id,
                "attachment_index": item.attachment_index,
                "sha256": digest,
                "mode": "video_frames" if media_kind == "video" else "image",
                "preparation_version": "native-media-v1",
                "question": question,
                "sampled_frames": len(images),
                "audio_analyzed": False if media_kind == "video" else None,
                "status": "prepared_for_main_agent",
            },
            images,
        )

    async def validate_images(self, images: tuple[ChatImage, ...]) -> None:
        """Recheck selected sources; never reread pixels or re-fetch a URL."""
        seen: dict[tuple[int, int], tuple[str | None, int | None, str | None, str | None]] = {}
        for image in images:
            if image.source != "history":
                continue
            if image.source_event_id is None or image.attachment_index is None:
                raise ConversationMediaError("attachment_source_invalid")
            key = (image.source_event_id, image.attachment_index)
            dependency = (
                image.conversation_id,
                image.generation,
                image.content_hash,
                image.expires_at,
            )
            if key in seen:
                if seen[key] != dependency:
                    raise ConversationMediaError("attachment_source_invalid")
                continue
            seen[key] = dependency
        sources = await self._sources(tuple(seen))
        for key, dependency in seen.items():
            conversation_id, generation, content_hash, expires_at = dependency
            item, current_generation, floor, _, _segments = sources[key]
            if (
                current_generation is None
                or floor is None
                or item.conversation_id != conversation_id
                or item.generation != generation
                or current_generation != generation
                or key[0] <= floor
            ):
                raise ConversationMediaError("attachment_scope_denied")
            if (
                item.cache_status != "cached"
                or item.content_sha256 != content_hash
                or item.expires_at is None
                or _aware(item.expires_at) <= datetime.now(UTC)
                or _aware(item.expires_at).isoformat() != expires_at
            ):
                raise ConversationMediaError("attachment_expired")

    async def _expire_page(self, now: datetime) -> None:
        model = ConversationMediaItemModel
        async with self.database.sessions() as reader:
            rows = tuple(
                await reader.execute(
                    select(
                        model.source_event_id,
                        model.attachment_index,
                        model.conversation_id,
                        model.generation,
                        model.cache_name,
                    )
                    .where(model.cache_status == "cached", model.expires_at <= now)
                    .order_by(model.expires_at, model.source_event_id, model.attachment_index)
                    .limit(_CLEANUP_PAGE)
                )
            )
        if not rows:
            return
        # Publication uses this same lock. Recheck state/expiry and the exact
        # published name after discovery, without loading payloads or history.
        async with self._lock, self.database.immediate_session() as writer:
            await writer.execute(
                update(model)
                .where(
                    model.cache_status == "cached",
                    model.expires_at <= now,
                    or_(
                        *(
                            and_(
                                model.source_event_id == row.source_event_id,
                                model.attachment_index == row.attachment_index,
                                model.conversation_id == row.conversation_id,
                                model.generation == row.generation,
                                model.cache_name == row.cache_name,
                            )
                            for row in rows
                        )
                    ),
                )
                .values(cache_status="expired")
            )

    async def _gc_cache_page(self, now: datetime) -> None:
        if self._gc_pending:
            entries, self._gc_pending = self._gc_pending, ()
        else:
            if self._gc_iterator is None:
                self._gc_iterator = _cache_entries(self.root)
            entries, finished = await _gc_io(_gc_page, self._gc_iterator)
            if finished:
                self._gc_iterator = None
        if not entries:
            return
        file_entries = tuple(
            entry
            for entry in entries
            if stat.S_ISREG(entry.token[2]) or stat.S_ISLNK(entry.token[2])
        )
        event_ids = set()
        for entry in file_entries:
            try:
                parts = entry.path.relative_to(self.root).parts
                if len(parts) == 3:
                    event_ids.add(int(parts[1]))
            except ValueError:
                continue
        # GC does not keep a full-cache live_paths projection. Look up only
        # exact event/name candidates; the source PK bounds this read.
        async with self._lock:
            async with self.database.sessions() as reader:
                live = (
                    tuple(
                        await reader.execute(
                            select(
                                ConversationMediaItemModel.conversation_id,
                                ConversationMediaItemModel.source_event_id,
                                ConversationMediaItemModel.cache_name,
                                ConversationMediaItemModel.cache_status,
                            )
                            .where(
                                ConversationMediaItemModel.source_event_id.in_(event_ids),
                                ConversationMediaItemModel.cache_name.in_(
                                    tuple(entry.path.name for entry in file_entries)
                                ),
                            )
                            .distinct()
                        )
                    )
                    if event_ids
                    else ()
                )
                events = (
                    tuple(
                        await reader.execute(
                            select(
                                ChatEventModel.id, ChatEventModel.canonical_conversation_id
                            ).where(ChatEventModel.id.in_(event_ids))
                        )
                    )
                    if event_ids
                    else ()
                )
            live_paths = {
                self.root / row.conversation_id / str(row.source_event_id) / row.cache_name
                for row in live
                if row.cache_status == "cached"
            }
            indexed_paths = {
                self.root / row.conversation_id / str(row.source_event_id) / row.cache_name
                for row in live
            }
            source_directories = {
                self.root / conversation_id / str(event_id) for event_id, conversation_id in events
            }
            # Edits replace the current index, not bytes already used by a
            # frozen request. Keep those source-owned versions for the same
            # file lifetime; deleted/forgotten sources retain no such ownership.
            live_paths.update(
                entry.path
                for entry in file_entries
                if entry.path.parent in source_directories
                and entry.path not in indexed_paths
                and entry.path.suffix != ".part"
                and entry.token[4] / 1_000_000_000 > now.timestamp() - _LIFETIME.total_seconds()
            )
            from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel

            async with self.database.sessions() as reader:
                hashes = await reader.scalars(
                    select(
                        func.json_extract(
                            ControlCommandReceiptModel.effective_state_json, "$.sha256"
                        )
                    ).where(
                        ControlCommandReceiptModel.operation_kind == "client.upload",
                        ControlCommandReceiptModel.status == "succeeded",
                        func.json_extract(
                            ControlCommandReceiptModel.effective_state_json, "$.expires_at"
                        )
                        > now.timestamp(),
                        func.json_extract(
                            ControlCommandReceiptModel.effective_state_json, "$.sha256"
                        ).in_(
                            tuple(
                                entry.path.stem
                                for entry in file_entries
                                if entry.path.parent == self.root / "uploads"
                            )
                        ),
                    )
                )
                live_paths.update(self.root / "uploads" / f"{digest}.blob" for digest in hashes)
            deletable = tuple(
                entry
                for entry in entries
                if (
                    stat.S_ISDIR(entry.token[2])
                    or (
                        entry.path not in live_paths
                        and (
                            entry.path.suffix != ".part"
                            or entry.token[4] / 1_000_000_000
                            < now.timestamp() - timedelta(hours=1).total_seconds()
                        )
                    )
                )
            )
            # Only immutable paths/stat tokens cross the thread boundary.
            # Re-stat before unlink, so an observed old file cannot delete a
            # replacement. No AsyncSession or ORM object leaves this loop.
            self._gc_pending = await _gc_io(_remove_cache_entries, deletable)

    async def cleanup(self) -> None:
        async with self._cleanup_lock:
            now = datetime.now(UTC)
            await self._expire_page(now)
            await self._gc_cache_page(now)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "conversation_media_cleanup_failed category=%s", type(exc).__name__
                )
