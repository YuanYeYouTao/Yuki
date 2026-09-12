"""Shared visual inspection of immutable workspace publications."""

from __future__ import annotations

import asyncio
from typing import Any

from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.vision.base import VisionProvider
from qq_ai_bot.vision.models import DownloadedMedia
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


class WorkspaceInspector:
    def __init__(
        self, store: WorkspaceStore, preprocessor: ImagePreprocessor, provider: VisionProvider
    ) -> None:
        self.store, self.preprocessor, self.provider = store, preprocessor, provider
        self.slot = asyncio.Semaphore(1)

    async def __call__(self, artifact_id: str, question: str) -> dict[str, Any]:
        if not 1 <= len(question) <= 2000:
            raise WorkspaceError("invalid_inspection_question")
        async with self.slot:
            metadata, data = await asyncio.to_thread(
                self.store.read_bytes, artifact_id, max_bytes=20 * 1024 * 1024
            )
            if not metadata["immutable"]:
                raise WorkspaceError("inspection_requires_published_artifact")
            prepared = await asyncio.to_thread(
                self.preprocessor.prepare,
                DownloadedMedia(
                    content=data,
                    content_type=None,
                    content_hash=metadata["sha256"],
                    byte_size=len(data),
                ),
                source="current",
            )
            control = current_work_control.get()
            if control is not None:
                await control.validate()
                await control.reserve_request(auxiliary=True)
            observation = await self.provider.analyze((prepared,), question)
            return {
                "artifact_id": artifact_id,
                "sha256": metadata["sha256"],
                "observation": observation.model_dump(mode="json"),
            }
