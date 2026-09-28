"""Read existing emoji originals and the Manager's explicitly mounted workspace."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.media_types import raster_type
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError, DownloadView
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.emoji.db_models import EmojiAssetModel
from qq_ai_bot.emoji.storage import EmojiStorage, EmojiStorageError
from qq_ai_bot.persistence.control_activity_query import _read_media
from qq_ai_bot.workspace.files import FileWorkspace
from qq_ai_bot.workspace.store import WorkspaceError


def workspace_bytes(root: Path, path: str) -> DownloadView:
    files = FileWorkspace(root)
    with files.open_file(path) as fd:
        if os.fstat(fd).st_size > 32 * 1024 * 1024:
            raise WorkspaceError("preview_too_large")
        digest, before = files.fingerprint(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        data = bytearray()
        while chunk := os.read(fd, 65536):
            data.extend(chunk)
            if len(data) > 32 * 1024 * 1024:
                raise WorkspaceError("preview_too_large")
        after_digest, after = files.fingerprint(fd)
        if digest != after_digest or before.st_size != after.st_size:
            raise WorkspaceError("file_changed_during_read")
    content = bytes(data)
    return DownloadView(Path(path).name[:128], content, raster_type(content))


def emoji_bytes(root: Path, relative_path: str, digest: str) -> bytes:
    path = EmojiStorage(root).resolve(relative_path)
    # Resolve may not turn a stored symlink into permission to read another file.
    if path != (root.absolute() / relative_path).absolute():
        raise ValueError("unsafe emoji path")
    return _read_media(path, digest)


async def emoji_download(
    reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    root: Path | None,
    asset_id: str,
) -> DownloadView:
    RequestId.parse(asset_id)
    if root is None:
        raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
    async with reader() as session:
        row = (
            await session.execute(
                select(EmojiAssetModel.relative_path, EmojiAssetModel.sha256).where(
                    EmojiAssetModel.id == asset_id
                )
            )
        ).one_or_none()
    if row is None:
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    try:
        data = await asyncio.to_thread(emoji_bytes, root, row.relative_path, row.sha256)
        media_type = raster_type(data)
        if media_type == "application/octet-stream":
            raise ValueError("invalid emoji image")
        return DownloadView(f"emoji-{asset_id[:8]}", data, media_type)
    except (OSError, ValueError, EmojiStorageError) as exc:
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND)) from exc
