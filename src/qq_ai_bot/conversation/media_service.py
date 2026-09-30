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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from qq_ai_bot.services.media_resolver import (
    MediaResolutionError,
    MediaResolver,
    OneBotMediaGateway,
)
from qq_ai_bot.services.video_frames import _run as run_parser
from qq_ai_bot.services.video_frames import sample_video
from qq_ai_bot.services.vision_service import VisionProcessingError
from qq_ai_bot.vision.base import VisionProvider
from qq_ai_bot.vision.models import DownloadedMedia, MediaReference

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


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ConversationMediaService:
    def __init__(
        self,
        database: Database,
        root: Path,
        resolver: MediaResolver,
        preprocessor: ImagePreprocessor,
        provider: VisionProvider | None,
    ) -> None:
        self.database = database
        self.root = root / "v1"
        self.resolver = resolver
        self.preprocessor = preprocessor
        self.provider = provider
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

    def submit(self, event_id: int, gateway: OneBotMediaGateway | None) -> None:
        if len(self._pending) >= 32:
            return
        task = asyncio.create_task(
            self._cache_event(event_id, gateway), name=f"conversation-media-{event_id}"
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _cache_event(self, event_id: int, gateway: OneBotMediaGateway | None) -> None:
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

    async def _source(
        self, event_id: int, index: int
    ) -> tuple[ConversationMediaItemModel, ChatEventModel, dict[str, Any]]:
        async with self.database.sessions() as session:
            item = await session.get(ConversationMediaItemModel, (event_id, index))
            event = await session.get(ChatEventModel, event_id)
            if (
                item is None
                or event is None
                or event.suppression_status != "keeper"
                or event.direction != "inbound"
                or event.canonical_conversation_id != item.conversation_id
            ):
                raise ConversationMediaError("attachment_not_found")
            try:
                segment = json.loads(event.segments_json)[item.segment_index]
            except (ValueError, TypeError, IndexError) as exc:
                raise ConversationMediaError("attachment_source_invalid") from exc
            if (
                not isinstance(segment, dict)
                or segment.get("type") != item.kind
                or not isinstance(segment.get("data"), dict)
            ):
                raise ConversationMediaError("attachment_source_invalid")
            return item, event, segment["data"]

    def _path(self, item: ConversationMediaItemModel) -> Path:
        if not item.cache_name or "/" in item.cache_name or "\\" in item.cache_name:
            raise ConversationMediaError("cache_missing")
        return self.root / item.conversation_id / str(item.source_event_id) / item.cache_name

    async def _cache(self, event_id: int, index: int, gateway: OneBotMediaGateway | None) -> Path:
        item, _, data = await self._source(event_id, index)
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
        try:
            if item.kind == "image":
                result = await self.resolver.resolve(reference, gateway)
                if len(result.content) > _MAX_FILE:
                    raise ConversationMediaError("attachment_too_large")
                await asyncio.to_thread(temporary.write_bytes, result.content)
            else:
                await self.resolver.download_attachment(
                    reference, temporary, max_download_bytes=_MAX_FILE
                )
            blob = await asyncio.to_thread(temporary.read_bytes)
            if not blob or len(blob) > _MAX_FILE:
                raise ConversationMediaError("attachment_invalid")
            if item.kind == "image":
                if blob.startswith(b"\xff\xd8\xff"):
                    extension = ".jpg"
                elif blob.startswith(b"\x89PNG"):
                    extension = ".png"
                elif blob.startswith(b"GIF8"):
                    extension = ".gif"
                elif blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
                    extension = ".webp"
                else:
                    raise ConversationMediaError("attachment_type_invalid")
            elif item.kind == "video":
                if blob[4:8] != b"ftyp":
                    raise ConversationMediaError("attachment_type_invalid")
                extension = ".mp4"
            else:
                extension = ".bin"
            digest = hashlib.sha256(blob).hexdigest()
            final_name = f"{index}-{digest}{extension}"
            final = directory / final_name
            async with self._lock:
                async with self.database.sessions() as session:
                    live = await session.get(ConversationMediaItemModel, (event_id, index))
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
                    occupied + len(blob) > _GLOBAL_BUDGET
                    or local + len(blob) > _CONVERSATION_BUDGET
                ):
                    raise ConversationMediaError("cache_budget_exhausted")
                os.replace(temporary, final)
                now = datetime.now(UTC)
                async with self.database.sessions() as session:
                    live = await session.get(ConversationMediaItemModel, (event_id, index))
                    if live is None or live.cache_status == "expired":
                        final.unlink(missing_ok=True)
                        raise ConversationMediaError("attachment_expired")
                    old_name = live.cache_name
                    live.cache_status = "cached"
                    live.cache_name = final_name
                    live.content_sha256 = digest
                    if live.cached_at is None:
                        live.cached_at = now
                        live.expires_at = now + _LIFETIME
                    await session.commit()
                if old_name and old_name != final_name:
                    (directory / old_name).unlink(missing_ok=True)
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
        gateway: OneBotMediaGateway | None,
    ) -> tuple[ConversationMediaItemModel, Path]:
        item, _, _ = await self._source(event_id, attachment_index)
        async with self.database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if (
                conversation is None
                or item.conversation_id != conversation_id
                or item.generation != conversation.generation
                or event_id <= conversation.starts_after_event_id
                or (generation is not None and generation != conversation.generation)
            ):
                raise ConversationMediaError("attachment_scope_denied")
        path = await self._cache(event_id, attachment_index, gateway)
        # Recheck after network work: /ai new may have advanced during download.
        async with self.database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if (
                conversation is None
                or item.generation != conversation.generation
                or event_id <= conversation.starts_after_event_id
            ):
                raise ConversationMediaError("attachment_scope_denied")
        return item, path

    async def inspect(
        self, item: ConversationMediaItemModel, path: Path, question: str
    ) -> dict[str, Any]:
        try:
            return await self._inspect(item, path, question)
        except (VisionProcessingError, ImagePreprocessingError) as exc:
            raise ConversationMediaError(exc.code) from exc

    async def _inspect(
        self, item: ConversationMediaItemModel, path: Path, question: str
    ) -> dict[str, Any]:
        if not 1 <= len(question) <= 2000:
            raise ConversationMediaError("invalid_question")
        digest = (
            item.content_sha256
            or hashlib.sha256(await asyncio.to_thread(path.read_bytes)).hexdigest()
        )

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
            result = json.loads(raw)
            if result.get("error"):
                raise ConversationMediaError(str(result["error"]))
            return {
                "event_id": item.source_event_id,
                "attachment_index": item.attachment_index,
                "sha256": digest,
                "mode": "document",
                "reader_version": "document-reader-v1",
                "result": result,
            }
        if self.provider is None:
            raise ConversationMediaError("vision_unavailable")
        control = current_work_control.get()
        if control is not None:
            await control.validate()
            await control.reserve_request(auxiliary=True)
        if media_kind == "video":
            frames = await sample_video(
                path,
                source="current",
                maximum=6,
                max_duration_seconds=120,
                sample_interval_seconds=15,
            )
            import base64

            payloads = [base64.b64decode(frame.data_url.split(",", 1)[1]) for frame in frames]
        else:
            payloads = [await asyncio.to_thread(path.read_bytes)]
        prepared = tuple(
            [
                await asyncio.to_thread(
                    self.preprocessor.prepare,
                    DownloadedMedia(
                        content=payload,
                        content_type=None,
                        content_hash=hashlib.sha256(payload).hexdigest(),
                        byte_size=len(payload),
                    ),
                    source="current",
                )
                for payload in payloads
            ]
        )
        observation = await self.provider.analyze(prepared, question)
        return {
            "event_id": item.source_event_id,
            "attachment_index": item.attachment_index,
            "sha256": digest,
            "mode": "video_frames" if media_kind == "video" else "image",
            "analysis_version": "conversation-media-v1",
            "sampled_frames": len(payloads),
            "audio_analyzed": False if media_kind == "video" else None,
            "observation": observation.model_dump(mode="json"),
        }

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
                .values(cache_status="expired", cache_name=None)
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
                            )
                            .where(
                                ConversationMediaItemModel.source_event_id.in_(event_ids),
                                ConversationMediaItemModel.cache_name.in_(
                                    tuple(entry.path.name for entry in file_entries)
                                ),
                                ConversationMediaItemModel.cache_status == "cached",
                            )
                            .distinct()
                        )
                    )
                    if event_ids
                    else ()
                )
            live_paths = {
                self.root / row.conversation_id / str(row.source_event_id) / row.cache_name
                for row in live
            }
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
