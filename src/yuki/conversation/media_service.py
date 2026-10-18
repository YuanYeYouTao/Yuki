"""Shared original-event media bytes with one lifetime owner and on-demand inspection."""

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
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from weakref import WeakSet, WeakValueDictionary

from sqlalchemy import and_, func, or_, select, text, tuple_, update

from yuki.admin.models import VisionRuntimeConfig
from yuki.capabilities.media import PreparedMediaData
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.media_index import indexed_media_segments
from yuki.domain.messages import AttachmentKind, ChatImage, InboundMessage, MessageAttachment
from yuki.gateway.media import MediaKind, PlatformMediaFile, PlatformMediaGateway
from yuki.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from yuki.services.media_process import MediaProcessError
from yuki.services.media_process import run_media_process as run_parser
from yuki.services.media_resolver import (
    MediaResolutionError,
    MediaResolver,
    PreparedAttachment,
    _media_io,
)
from yuki.services.native_media import (
    MAX_IMAGE_BYTES,
    NativeMediaPreparer,
    image_extension,
    native_sticker_facts,
)
from yuki.services.video_frames import is_video_header
from yuki.services.vision_service import VisionProcessingError
from yuki.vision.models import MediaReference

_LIFETIME = timedelta(hours=24)
_DEFAULT_VIDEO_READ_BYTES = 209_715_200
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
        self, file: str, kind: MediaKind, *, max_download_bytes: int | None
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


def _cache_file_facts(path: Path) -> _CacheFileFacts:
    """Hash the streamed original before opening a writer."""
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or not initial.st_size:
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
        self._prepared: WeakSet[PreparedAttachment] = WeakSet()
        self._originals: WeakValueDictionary[tuple[int, int, bool], PreparedAttachment] = (
            WeakValueDictionary()
        )
        self._cache_tasks: dict[tuple[int, int, str, str | None], asyncio.Task[Path]] = {}
        self._staging_paths: set[Path] = set()

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self.cleanup()
        self._cleaner = asyncio.create_task(self._cleanup_loop(), name="conversation-media-cleanup")

    async def close(self) -> None:
        if self._cleaner is not None:
            self._cleaner.cancel()
        cache_tasks = tuple(self._cache_tasks.values())
        for task in (*tuple(self._pending), *cache_tasks):
            task.cancel()
        await asyncio.gather(
            *(tuple(self._pending) + cache_tasks + ((self._cleaner,) if self._cleaner else ())),
            return_exceptions=True,
        )
        self._cleaner = None
        await asyncio.gather(*(original.close() for original in self._prepared))
        self._prepared.clear()
        async with self._cleanup_lock:
            if self._gc_iterator is not None:
                closer = getattr(self._gc_iterator, "close", None)
                if closer is not None:
                    await _gc_io(closer)
                self._gc_iterator = None
            self._gc_pending = ()

    def prepare_message(
        self,
        message: InboundMessage,
        gateway: PlatformMediaGateway | None,
        *,
        source_segments: tuple[dict[str, Any], ...],
    ) -> InboundMessage:
        """Distribute one original per admitted attachment, without waiting for cache I/O."""
        kinds = {
            AttachmentKind.IMAGE,
            AttachmentKind.VIDEO,
            AttachmentKind.FILE,
            AttachmentKind.AUDIO,
        }

        def original_reply(
            kind: AttachmentKind, label: str, position: int, data: dict[str, Any]
        ) -> MessageAttachment:
            preview = data.get("preview")
            return MessageAttachment(
                kind,
                label,
                segment_index=position,
                source="reply",
                file=data.get("file"),
                url=data.get("url"),
                filename=data.get("name") or data.get("filename"),
                summary=data.get("summary"),
                file_size=data.get("file_size"),
                sub_type=data.get("sub_type"),
                emoji_id=data.get("emoji_id"),
                emoji_package_id=data.get("emoji_package_id"),
                is_sticker=bool(data.get("is_sticker")),
                sticker_format=data.get("sticker_format"),
                mime_type=data.get("mime_type"),
                width=data.get("width"),
                height=data.get("height"),
                key=data.get("key"),
                preview=original_reply(AttachmentKind.IMAGE, "sticker_preview", position, preview)
                if isinstance(preview, dict)
                else None,
            )

        replies = (
            tuple(
                original_reply(
                    AttachmentKind.AUDIO if kind == "record" else AttachmentKind(kind),
                    kind,
                    position,
                    data,
                )
                for _index, position, kind, data in indexed_media_segments(message.reply_segments)
            )
            if message.reply_to_event_id is not None
            else message.reply_attachments
        )
        if not any(attachment.kind in kinds for attachment in (*message.attachments, *replies)):
            return (
                message
                if replies == message.reply_attachments
                else replace(message, reply_attachments=replies)
            )
        original_segments = json.dumps(source_segments, ensure_ascii=False, separators=(",", ":"))

        def prepare(
            attachment: MessageAttachment,
            segments: str,
            *,
            original: bool = True,
        ) -> MessageAttachment:
            if attachment.kind not in kinds:
                return attachment
            shared = PreparedAttachment(
                self.resolver,
                MediaReference(
                    file=attachment.file,
                    url=attachment.url,
                    source="reply" if attachment.source == "reply" else "current",
                ),
                _EventMediaGateway(self.database, self.gateway_registry, message.reply_to_event_id)
                if attachment.source == "reply" and message.reply_to_event_id is not None
                else gateway,
                kind=cast(MediaKind, attachment.kind.value),
                local_source=attachment.local_source,
                source_segments=segments,
            )
            self._prepared.add(shared)
            event_id = (
                message.reply_to_event_id
                if attachment.source == "reply"
                else message.source_event_id
            )
            if event_id is None and attachment.source == "reply":
                event_id = message.source_event_id
            if original and event_id is not None:
                self._originals[
                    (
                        event_id,
                        attachment.segment_index,
                        attachment.source == "reply" and message.reply_to_event_id is None,
                    )
                ] = shared
            return replace(
                attachment,
                preview=prepare(
                    attachment.preview,
                    segments,
                    original=False,
                )
                if attachment.preview is not None
                else None,
                prepared_media=shared,
            )

        attachments = tuple(
            prepare(attachment, original_segments) for attachment in message.attachments
        )
        if replies:
            reply_segments = json.dumps(
                message.reply_segments, ensure_ascii=False, separators=(",", ":")
            )
            replies = tuple(prepare(attachment, reply_segments) for attachment in replies)
            if message.reply_to_event_id is not None:
                self.submit(message.reply_to_event_id, None)
        return replace(message, attachments=attachments, reply_attachments=replies)

    def submit(
        self,
        event_id: int,
        gateway: PlatformMediaGateway | None,
        *,
        message: InboundMessage | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._cache_event(event_id, gateway, message), name=f"conversation-media-{event_id}"
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def source_current(self, event_id: int, segment_index: int, source_segments: str) -> bool:
        """Accept promotion from one original event/index snapshot, without writing provenance."""
        async with self.database.sessions() as session:
            segments = await session.scalar(
                select(ChatEventModel.segments_json)
                .join(
                    ConversationMediaItemModel,
                    ConversationMediaItemModel.source_event_id == ChatEventModel.id,
                )
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == ConversationMediaItemModel.conversation_id,
                )
                .where(
                    ChatEventModel.id == event_id,
                    ConversationMediaItemModel.segment_index == segment_index,
                    ConversationMediaItemModel.source_snapshot_json.is_(None),
                    ChatEventModel.canonical_conversation_id
                    == ConversationMediaItemModel.conversation_id,
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.suppression_status == "keeper",
                    ConversationMediaItemModel.generation == CanonicalConversationModel.generation,
                    ChatEventModel.id > CanonicalConversationModel.starts_after_event_id,
                )
            )
        return segments is not None and json.loads(segments) == json.loads(source_segments)

    async def _cache_event(
        self,
        event_id: int,
        gateway: PlatformMediaGateway | None,
        message: InboundMessage | None = None,
    ) -> None:
        originals = (
            {
                (attachment.source, attachment.segment_index): attachment.prepared_media
                for attachment in (*message.attachments, *message.reply_attachments)
            }
            if message is not None
            else {}
        )
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
                    await self._cache(
                        item.source_event_id,
                        item.attachment_index,
                        gateway,
                        prepared_media=originals.get(
                            (
                                "reply" if item.source_snapshot_json is not None else "current",
                                item.segment_index,
                            )
                        ),
                    )
            except (ConversationMediaError, MediaResolutionError, OSError, ValueError):
                # Failed prefetch remains uncached and can be retried on inspection.
                continue

    async def _sources(
        self, keys: tuple[tuple[int, int], ...]
    ) -> dict[
        tuple[int, int],
        tuple[ConversationMediaItemModel, dict[str, Any], str],
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
                await session.scalars(
                    select(model)
                    .join(ChatEventModel, ChatEventModel.id == model.source_event_id)
                    .where(
                        tuple_(model.source_event_id, model.attachment_index).in_(selected),
                        ChatEventModel.suppression_status == "keeper",
                        ChatEventModel.direction == "inbound",
                        ChatEventModel.canonical_conversation_id == model.conversation_id,
                    )
                )
            ).all()
            metadata = {(row.source_event_id, row.attachment_index): row for row in rows}
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
        parsed: dict[str, Any] = {}
        sources: dict[
            tuple[int, int],
            tuple[ConversationMediaItemModel, dict[str, Any], str],
        ] = {}
        for key in keys:
            row = metadata.get(key)
            if row is None:
                raise ConversationMediaError("attachment_not_found")
            item = row
            try:
                source_segments = item.source_snapshot_json or raw_segments[key[0]]
                if source_segments not in parsed:
                    parsed[source_segments] = json.loads(source_segments)
                segment = (
                    parsed[source_segments]
                    if item.source_snapshot_json is not None
                    else parsed[source_segments][item.segment_index]
                )
            except (ValueError, TypeError, IndexError) as exc:
                raise ConversationMediaError("attachment_source_invalid") from exc
            if (
                not isinstance(segment, dict)
                or segment.get("type") != item.kind
                or not isinstance(segment.get("data"), dict)
            ):
                raise ConversationMediaError("attachment_source_invalid")
            sources[key] = (item, segment["data"], raw_segments[key[0]])
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
        source: tuple[ConversationMediaItemModel, dict[str, Any], str] | None = None,
        prepared_media: PreparedAttachment | None = None,
    ) -> Path:
        if source is None:
            source = (await self._sources(((event_id, index),)))[(event_id, index)]
        item = source[0]
        key = event_id, index, source[-1], item.source_snapshot_json
        task = self._cache_tasks.get(key)
        if task is None:
            explicit_original = prepared_media is not None
            prepared_media = prepared_media or self._originals.get(
                (event_id, item.segment_index, item.source_snapshot_json is not None)
            )
            if prepared_media is not None:
                prepared_segment = json.loads(prepared_media.source_segments)[item.segment_index]
                if (
                    prepared_segment.get("type") != item.kind
                    or prepared_segment.get("data") != source[1]
                ):
                    if explicit_original:
                        raise ConversationMediaError("attachment_source_changed")
                    prepared_media = None
            task = asyncio.create_task(
                self._cache_original(
                    event_id, index, gateway, source=source, prepared_media=prepared_media
                ),
                name=f"conversation-media-original:{event_id}:{index}",
            )
            self._cache_tasks[key] = task
            task.add_done_callback(lambda done: self._finish_cache(key, done))
        return await asyncio.shield(task)

    def _finish_cache(
        self, key: tuple[int, int, str, str | None], task: asyncio.Task[Path]
    ) -> None:
        if self._cache_tasks.get(key) is task:
            del self._cache_tasks[key]
        if not task.cancelled():
            task.exception()

    async def _cache_original(
        self,
        event_id: int,
        index: int,
        gateway: PlatformMediaGateway | None,
        *,
        source: tuple[ConversationMediaItemModel, dict[str, Any], str],
        prepared_media: PreparedAttachment | None,
    ) -> Path:
        item, data, source_segments = source
        if item.cache_name:
            path = self._path(item)
            if path.is_file() and not path.is_symlink():
                return path
        if item.cache_status == "expired" or (
            item.expires_at is not None and _aware(item.expires_at) <= datetime.now(UTC)
        ):
            raise ConversationMediaError("attachment_expired")
        directory = self.root / item.conversation_id / str(event_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = directory / f"{uuid4().hex}.part"
        reference = MediaReference(file=data.get("file"), url=data.get("url"), source="current")
        gateway = gateway or _EventMediaGateway(self.database, self.gateway_registry, event_id)
        self._staging_paths.add(temporary)
        try:
            if prepared_media is not None:
                await prepared_media.download(temporary)
            else:
                await self.resolver.download_attachment(
                    reference,
                    temporary,
                    max_download_bytes=None,
                    gateway=gateway,
                    kind="audio" if item.kind == "record" else cast(MediaKind, item.kind),
                )
            facts = await _media_io(_cache_file_facts, temporary)
            extension: str | None
            if facts.head.startswith(b"\x1f\x8b"):
                extension = ".gz"
            elif item.kind in {"image", "video"}:
                extension = image_extension(facts.head)
                if extension is None and is_video_header(facts.head):
                    extension = ".webm" if facts.head.startswith(b"\x1aE\xdf\xa3") else ".mp4"
                if extension is None:
                    extension = ".bin"
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
                    if live is None or live.source_snapshot_json != item.source_snapshot_json:
                        raise ConversationMediaError("attachment_source_changed")
                    if live.cache_status == "expired":
                        raise ConversationMediaError("attachment_expired")
                    if live.cache_status == "cached" and live.cache_name:
                        existing = self._path(live)
                        if existing.is_file():
                            return existing
                        if live.expires_at and _aware(live.expires_at) <= datetime.now(UTC):
                            raise ConversationMediaError("attachment_expired")

                await _media_io(_publish_cache_file, temporary, final, facts)
                now = datetime.now(UTC)
                async with self.database.immediate_session() as session:
                    live = await session.get(ConversationMediaItemModel, (event_id, index))
                    event = await session.get(ChatEventModel, event_id)
                    if event is None or event.segments_json != source_segments:
                        raise ConversationMediaError("attachment_source_changed")
                    if live is None or live.source_snapshot_json != item.source_snapshot_json:
                        raise ConversationMediaError("attachment_source_changed")
                    if live.cache_status == "expired":
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
            try:
                await _media_io(partial(temporary.unlink, missing_ok=True))
            finally:
                self._staging_paths.discard(temporary)

    async def authorized_path(
        self,
        *,
        event_id: int,
        attachment_index: int,
    ) -> tuple[ConversationMediaItemModel, Path]:
        key = (event_id, attachment_index)
        source = (await self._sources((key,)))[key]
        item, _, source_segments = source
        path = await self._cache(event_id, attachment_index, None, source=source)
        # Keep the exact observed source when publication waited for network I/O.
        async with self.database.sessions() as session:
            event = await session.get(ChatEventModel, event_id)
            live = await session.get(ConversationMediaItemModel, key)
            if (
                event is None
                or event.segments_json != source_segments
                or live is None
                or live.source_snapshot_json != item.source_snapshot_json
            ):
                raise ConversationMediaError("attachment_source_changed")
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
        except (VisionProcessingError, ImagePreprocessingError, MediaResolutionError) as exc:
            raise ConversationMediaError(exc.code) from exc

    async def _validated_cached_path(
        self, item: ConversationMediaItemModel, path: Path
    ) -> ConversationMediaItemModel:
        key = (item.source_event_id, item.attachment_index)
        live, _data, _segments = (await self._sources((key,)))[key]
        if not live.content_sha256 or not await _media_io(path.is_file):
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
        source_key = (item.source_event_id, item.attachment_index)
        _, source_data, source_segments = (await self._sources((source_key,)))[source_key]
        sticker = await _media_io(native_sticker_facts, path)
        is_tgs = sticker is not None
        preview = source_data.get("preview") if is_tgs else None
        if is_tgs and not isinstance(preview, dict):
            return {
                "event_id": item.source_event_id,
                "attachment_index": item.attachment_index,
                "sha256": digest,
                "mode": "sticker_metadata",
                "format": "TGS",
                "width": sticker.get("width") if sticker else None,
                "height": sticker.get("height") if sticker else None,
                "summary": source_data.get("summary"),
                "status": "preview_unavailable",
            }

        def content_kind() -> str:
            with path.open("rb") as stream:
                header = stream.read(12)
            if is_video_header(header):
                return "video"
            if image_extension(header) is not None:
                return "image"
            return "file"

        media_kind = "sticker_preview" if is_tgs else await asyncio.to_thread(content_kind)
        if media_kind == "file":
            original_name = source_data.get("name") or source_data.get("filename")
            suffix = Path(str(original_name or item.display_name)).suffix.lower()
            try:
                raw = await run_parser(
                    sys.executable,
                    "-m",
                    "yuki.services.document_reader",
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
        if media_kind == "sticker_preview":
            assert isinstance(preview, dict)
            downloaded = await self.resolver.resolve(
                MediaReference(file=preview["file"], source="current"),
                _EventMediaGateway(self.database, self.gateway_registry, item.source_event_id),
            )
            images = await asyncio.to_thread(
                self.preparer.prepare_image,
                downloaded,
                source="history",
                max_frames=runtime.max_frames_per_turn if runtime else None,
            )
        elif media_kind == "video":
            limit = runtime.video_max_download_bytes if runtime else _DEFAULT_VIDEO_READ_BYTES
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
                version=hashlib.sha256(
                    (item.source_snapshot_json or source_segments).encode()
                ).hexdigest()
                if is_tgs
                else None,
            )
            for image in images
        )
        await self.validate_images(images)
        return PreparedMediaData(
            {
                "event_id": item.source_event_id,
                "attachment_index": item.attachment_index,
                "sha256": digest,
                "mode": "video_frames" if media_kind == "video" else media_kind,
                "preparation_version": "native-media-v1",
                "question": question,
                "sampled_frames": len(images),
                "audio_analyzed": False if media_kind == "video" else None,
                "status": "prepared_for_main_agent",
                **(
                    {
                        "format": "TGS",
                        "preview": "platform_static_thumbnail",
                        "summary": source_data.get("summary"),
                    }
                    if is_tgs
                    else {}
                ),
            },
            images,
        )

    async def validate_images(self, images: tuple[ChatImage, ...]) -> None:
        """Recheck selected sources; never reread pixels or re-fetch a URL."""
        seen: dict[tuple[int, int], tuple[str | None, str | None, str | None]] = {}
        for image in images:
            if image.source != "history":
                continue
            if image.source_event_id is None or image.attachment_index is None:
                raise ConversationMediaError("attachment_source_invalid")
            key = (image.source_event_id, image.attachment_index)
            dependency = (
                image.conversation_id,
                image.content_hash,
                image.version,
            )
            if key in seen:
                if seen[key] != dependency:
                    raise ConversationMediaError("attachment_source_invalid")
                continue
            seen[key] = dependency
        sources = await self._sources(tuple(seen))
        for key, dependency in seen.items():
            conversation_id, content_hash, source_version = dependency
            item, _, _segments = sources[key]
            if item.conversation_id != conversation_id:
                raise ConversationMediaError("attachment_source_changed")
            if (
                source_version is not None
                and source_version
                != hashlib.sha256((item.source_snapshot_json or _segments).encode()).hexdigest()
            ):
                raise ConversationMediaError("attachment_source_changed")
            if item.content_sha256 != content_hash or not await _media_io(self._path(item).is_file):
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
            from yuki.conversation.canonical_db_models import ControlCommandReceiptModel

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
            live_paths.update(self._staging_paths)
            deletable = tuple(
                entry
                for entry in entries
                if (stat.S_ISDIR(entry.token[2]) or entry.path not in live_paths)
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
