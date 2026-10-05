"""Prepare selected immutable artifacts or authorized Manager file versions."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.capabilities.media import PreparedMediaData
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from qq_ai_bot.services.native_media import MAX_IMAGE_BYTES, NativeMediaPreparer
from qq_ai_bot.services.vision_service import VisionProcessingError
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


class WorkspaceInspector:
    def __init__(
        self,
        store: WorkspaceStore,
        preprocessor: ImagePreprocessor,
        *,
        max_prepared_bytes: int = 6_291_456,
    ) -> None:
        self.store = store
        self.preparer = NativeMediaPreparer(preprocessor, max_bytes=max_prepared_bytes)
        self.slot = asyncio.Semaphore(1)

    async def __call__(
        self,
        artifact_id: str | None,
        question: str,
        *,
        runtime: VisionRuntimeConfig | None = None,
        file_metadata: dict[str, Any] | None = None,
        data: bytes | None = None,
    ) -> PreparedMediaData:
        if not 1 <= len(question) <= 2000:
            raise WorkspaceError("invalid_inspection_question")
        async with self.slot:
            if artifact_id is not None:
                metadata, data = await asyncio.to_thread(
                    self.store.read_bytes, artifact_id, max_bytes=MAX_IMAGE_BYTES
                )
                if not metadata["immutable"]:
                    raise WorkspaceError("inspection_requires_published_artifact")
            else:
                if file_metadata is None or data is None:
                    raise WorkspaceError("workspace_source_missing")
                metadata = file_metadata
            assert data is not None
            digest = hashlib.sha256(data).hexdigest()
            if len(data) > MAX_IMAGE_BYTES or digest != metadata.get(
                "sha256", metadata.get("version")
            ):
                raise WorkspaceError("workspace_media_invalid")
            try:
                if data[4:8] == b"ftyp":
                    with TemporaryDirectory(prefix="yuki-workspace-video-") as directory:
                        path = Path(directory) / "input.mp4"
                        await asyncio.to_thread(path.write_bytes, data)
                        images = await self.preparer.prepare_video(
                            path, source="workspace", runtime=runtime
                        )
                    mode = "video_frames"
                else:
                    images = await asyncio.to_thread(
                        self.preparer.prepare_image,
                        data,
                        source="workspace",
                        max_frames=runtime.max_frames_per_turn if runtime else None,
                    )
                    mode = "image"
            except (ImagePreprocessingError, VisionProcessingError) as exc:
                raise WorkspaceError(exc.code) from exc
            expires_at = metadata.get("expires_at")
            images = tuple(
                replace(
                    image,
                    artifact_id=artifact_id,
                    workspace_path=metadata.get("path"),
                    version=digest,
                    content_hash=digest,
                    expires_at=datetime.fromtimestamp(expires_at, UTC).isoformat()
                    if expires_at is not None
                    else None,
                )
                for image in images
            )
            return PreparedMediaData(
                {
                    **{k: metadata[k] for k in ("artifact_id", "path", "version") if k in metadata},
                    "sha256": digest,
                    "question": question,
                    "mode": mode,
                    "sampled_frames": len(images),
                    "status": "prepared_for_main_agent",
                    "audio_analyzed": False if mode == "video_frames" else None,
                },
                images,
            )

    async def validate_artifact(self, image: ChatImage) -> None:
        def metadata() -> dict[str, Any]:
            with self.store._transaction(write=False) as db:
                return self.store._metadata(self.store._row(db, image.artifact_id or ""))

        item = await asyncio.to_thread(metadata)
        if not item["immutable"] or item["sha256"] != image.version:
            raise WorkspaceError("workspace_media_version_changed")
