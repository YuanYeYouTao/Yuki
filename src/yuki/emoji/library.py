"""Ordinary shared sticker files and optional background descriptions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from yuki.admin.models import VisionRuntimeConfig
from yuki.conversation.media_index import indexed_media_segments
from yuki.conversation.media_service import ConversationMediaError
from yuki.domain.messages import InboundMessage, MessageAttachment
from yuki.emoji.media import extension, header_facts, inspect_content
from yuki.emoji.models import EmojiAsset
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import _media_io
from yuki.services.video_frames import sample_video
from yuki.vision.base import VisionProvider
from yuki.vision.models import (
    DownloadedMedia,
    PreparedFrame,
    PreparedVisualInput,
    VisionAnalysisOptions,
)
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError

logger = logging.getLogger(__name__)


def _naming_facts(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        facts = header_facts(stream)
    if facts is None:
        return {"image_format": "BIN", "mime_type": "application/octet-stream"}
    return facts


def _read_original(path: Path, expected: str) -> bytes:
    """Read one original after the winner is known, and reject a changed source."""
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with path.open("rb") as stream:
        while piece := stream.read(65536):
            digest.update(piece)
            chunks.append(piece)
    if digest.hexdigest() != expected:
        raise ValueError("attachment_source_changed")
    return b"".join(chunks)


def snapshot_identity(platform: str, digest: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"yuki:sticker:{platform}:{digest}"))


class EmojiLibrary:
    def __init__(
        self,
        workspace: WorkspaceService,
        *,
        preprocessor: ImagePreprocessor | None = None,
        provider: VisionProvider | None = None,
        vision_runtime: Callable[[], Awaitable[VisionRuntimeConfig]] | None = None,
    ) -> None:
        self.workspace = workspace
        self.preprocessor, self.provider = preprocessor, provider
        self.vision_runtime = vision_runtime
        self._tasks: set[asyncio.Task[Any]] = set()

    def submit(self, message: InboundMessage, *, source_event_id: int) -> None:
        if any(item.is_sticker and item.source == "current" for item in message.attachments):
            self._submit(self.collect_message(message, source_event_id=source_event_id))

    def _submit(self, operation: Any) -> None:
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("sticker_background_failed category=%s", type(error).__name__)

    async def close(self) -> None:
        while self._tasks:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _index(self, command: str, **arguments: Any) -> dict[str, Any]:
        result = await self.workspace.sandbox.execute(
            "sticker_index", {"command": command, **arguments}, request_id=str(uuid4())
        )
        if result.get("error"):
            raise WorkspaceError(str(result["error"]))
        return result

    async def collect_message(
        self, message: InboundMessage, *, source_event_id: int
    ) -> tuple[EmojiAsset, ...]:
        media = self.workspace.conversation_media
        indices = {
            position: index
            for index, position, _kind, _data in indexed_media_segments(message.segments)
        }
        results = []
        for attachment in message.attachments:
            if (
                not attachment.is_sticker
                or attachment.source != "current"
                or attachment.prepared_media is None
            ):
                continue
            try:
                index = indices[attachment.segment_index]
                item, path = await media.authorized_path(
                    event_id=source_event_id,
                    attachment_index=index,
                    expected_source_segments=attachment.prepared_media.source_segments,
                )
                digest = cast(str, item.content_sha256)
                results.append(
                    await self.store_cached(
                        path,
                        shared_path="/inputs/media-cache/v1/"
                        + path.relative_to(media.root).as_posix(),
                        sha256=digest,
                        platform=message.platform,
                        is_sticker=True,
                        request_id=f"sticker:{source_event_id}:{attachment.segment_index}:{digest}",
                        source_key=f"attachment:{source_event_id}:{index}",
                        tags=(attachment.summary.strip(),)
                        if attachment.summary and attachment.summary.strip()
                        else (),
                        preview=attachment.preview,
                    )
                )
            except (ConversationMediaError, OSError, ValueError, WorkspaceError) as exc:
                logger.warning("sticker_collection_failed category=%s", type(exc).__name__)
        return tuple(results)

    async def store_cached(
        self,
        cached_path: Path,
        *,
        shared_path: str,
        sha256: str,
        platform: str,
        source_key: str,
        request_id: str,
        is_sticker: bool = False,
        tags: tuple[str, ...] = (),
        preview: MessageAttachment | None = None,
    ) -> EmojiAsset:
        digest = sha256
        previous = await self._index("lookup", platform=platform, digest=digest)
        if previous["asset"] is not None:
            if is_sticker and not previous["asset"].get("is_sticker"):
                previous = await self._index(
                    "catalog",
                    platform=platform,
                    path=previous["asset"]["relative_path"],
                    is_sticker=True,
                )
            return EmojiAsset.model_validate(previous["asset"])
        facts = await _media_io(_naming_facts, cached_path)
        name = f"{digest}.{extension(facts['image_format'])}"
        destination = f"stickers/{platform}/files/{name}"
        metadata = await self.workspace.copy_from_cache(
            path=shared_path,
            destination=destination,
            request_id=request_id,
            expected_version=digest,
            source_key=source_key,
        )
        if metadata.get("error"):
            raise WorkspaceError(str(metadata["error"]))
        result = await self._index(
            "catalog",
            platform=platform,
            path=str(metadata["path"]).removeprefix("/workspace/"),
            tags=list(tags),
            is_sticker=is_sticker,
        )
        asset = EmojiAsset.model_validate(result["asset"])
        if (
            result["created"]
            and self.provider is not None
            and self.preprocessor is not None
            and self.vision_runtime is not None
        ):
            self._submit(
                self.describe(
                    asset,
                    source_path=cached_path,
                    expected_sha=digest,
                    preview=preview,
                )
            )
        return asset

    async def rebuild(self, *, platform: str | None = None) -> dict[str, Any]:
        return await self._index("rebuild", platform=platform)

    async def search(self, query: str, *, platform: str, limit: int = 20) -> tuple[EmojiAsset, ...]:
        result = await self._index("search", platform=platform, query=query, limit=limit)
        while result.get("needs_build"):
            await self.rebuild(platform=platform)
            result = await self._index("search", platform=platform, query=query, limit=limit)
        return tuple(EmojiAsset.model_validate(item) for item in result["items"])

    async def select(self, goal: str, emotion: str = "", *, platform: str) -> EmojiAsset | None:
        candidates = await self.search(" ".join((goal, emotion)), platform=platform, limit=1)
        return candidates[0] if candidates else None

    async def publish(self, asset: EmojiAsset, *, request_id: str) -> dict[str, Any]:
        identity = snapshot_identity(asset.platform, asset.sha256)

        def existing_snapshot() -> dict[str, Any] | None:
            try:
                with self.workspace.store._transaction(write=False) as db:
                    row = self.workspace.store._row(db, identity)
                    return self.workspace.store._metadata(row)
            except WorkspaceError as error:
                if str(error) == "artifact_not_found":
                    return None
                raise

        metadata = await _media_io(existing_snapshot)
        if metadata is None:
            if asset.relative_path is None:
                raise WorkspaceError("sticker_file_missing")
            metadata = await self.workspace.publish_for_send(
                path=asset.relative_path,
                expected_version=asset.sha256,
                name=f"{asset.sha256}.{extension(asset.image_format)}",
                artifact_id=identity,
                source_key=f"sticker:{asset.platform}:{asset.sha256}",
                request_id=f"sticker-publish:{request_id}",
            )
            if metadata.get("error"):
                raise WorkspaceError(
                    "file_mutation_unknown_read_current_version"
                    if metadata.get("uncertain")
                    else str(metadata["error"])
                )
        return {
            **metadata,
            "mime_type": asset.mime_type,
            "animated": asset.animated,
            "is_sticker": asset.is_sticker,
            "emoji_id": asset.id,
        }

    async def describe(
        self,
        asset: EmojiAsset,
        *,
        content: bytes | None = None,
        preview: MessageAttachment | None = None,
        source_path: Path | None = None,
        expected_sha: str | None = None,
    ) -> EmojiAsset:
        if self.provider is None or self.preprocessor is None or self.vision_runtime is None:
            return asset
        is_preview = asset.image_format == "TGS"
        if is_preview:
            if preview is None or preview.prepared_media is None:
                return asset
            preview_path, _ = await preview.prepared_media.source()
            try:
                content = await _media_io(preview_path.read_bytes)
            except OSError:
                return asset
        elif content is None:
            if source_path is None:
                return asset
            try:
                content = await _media_io(
                    _read_original, source_path, expected_sha or asset.sha256
                )
            except (OSError, ValueError):
                return asset
        if content is None:
            return asset
        runtime = await self.vision_runtime()
        facts = await asyncio.to_thread(inspect_content, content) if is_preview else None
        downloaded = DownloadedMedia(
            content=content,
            content_type=facts["mime_type"] if facts else asset.mime_type,
            content_hash=facts["sha256"] if facts else asset.sha256,
            byte_size=len(content),
        )
        if asset.mime_type.startswith("video/"):
            frames = await sample_video(
                downloaded,
                source="current",
                maximum=min(runtime.max_frames_per_turn, runtime.video_max_frames),
                max_duration_seconds=runtime.video_max_duration_seconds,
                sample_interval_seconds=runtime.video_sample_interval_seconds,
            )

            def prepare_frames() -> PreparedVisualInput:
                import base64

                prepared_frames = []
                for index, frame in enumerate(frames):
                    data = base64.b64decode(frame.data_url.split(",", 1)[1])
                    frame_facts = inspect_content(data)
                    prepared_frames.append(
                        PreparedFrame(
                            content_hash=frame_facts["sha256"],
                            mime_type=frame_facts["mime_type"],
                            width=frame_facts["width"],
                            height=frame_facts["height"],
                            frame_index=index,
                            frame_count=len(frames),
                            data_url=frame.data_url,
                        )
                    )
                return PreparedVisualInput(
                    media_hash=asset.sha256,
                    frames=tuple(prepared_frames),
                    animated=True,
                    source="current",
                )

            prepared = await asyncio.to_thread(prepare_frames)
        else:
            prepared = await asyncio.to_thread(
                self.preprocessor.prepare,
                downloaded,
                source="current",
                max_frames=runtime.gif_max_frames,
            )
        instruction = (
            "为共享表情图片建立检索资料：描述图片本身、图片文字、动作和表达含义，"
            "区分大笑、反讽、幸灾乐祸等。emotion_tags和usage_scenarios填写简短语义标签。"
            "不要加入聊天人物或原消息语境。"
        )
        if is_preview:
            instruction += "本次输入为动画贴纸的静态平台预览，只描述可见画面，不推断未展示的动画。"
        observation = await self.provider.analyze(
            (prepared,),
            instruction,
            options=VisionAnalysisOptions(
                thinking_enabled=runtime.thinking_enabled,
                thinking_budget=runtime.thinking_budget,
            ),
        )
        tags = tuple(
            dict.fromkeys(
                tag
                for item in observation.items
                for tag in (*item.emotion_tags, *item.usage_scenarios)
                if tag.strip()
            )
        )
        result = await self._index(
            "annotate",
            asset=asset.model_dump(mode="json"),
            description=observation.overall_description,
            tags=list(tags),
            ocr_text="\n".join(item.ocr_text for item in observation.items if item.ocr_text),
        )
        return EmojiAsset.model_validate(result["asset"])
