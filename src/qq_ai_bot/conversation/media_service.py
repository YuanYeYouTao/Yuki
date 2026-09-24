"""Conversation-bound, fixed-lifetime media bytes and on-demand inspection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

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

    async def cleanup(self) -> None:
        async with self._lock:
            now = datetime.now(UTC)
            async with self.database.sessions() as session:
                expired = (
                    await session.scalars(
                        select(ConversationMediaItemModel).where(
                            ConversationMediaItemModel.cache_status == "cached",
                            ConversationMediaItemModel.expires_at <= now,
                        )
                    )
                ).all()
                paths = []
                for item in expired:
                    paths.append(self._path(item))
                    item.cache_status = "expired"
                    item.cache_name = None
                await session.commit()
            for path in paths:
                path.unlink(missing_ok=True)
                for parent in (path.parent, path.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
            # An interrupted download is never a valid cached object.
            for part in self.root.rglob("*.part"):
                try:
                    if datetime.fromtimestamp(part.stat().st_mtime, UTC) + timedelta(hours=1) < now:
                        part.unlink(missing_ok=True)
                except OSError:
                    continue

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "conversation_media_cleanup_failed category=%s", type(exc).__name__
                )
