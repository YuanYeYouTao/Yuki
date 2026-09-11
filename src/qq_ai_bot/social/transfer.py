"""Short-lived, read-only gateway handoff, separate from the shared workspace."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from qq_ai_bot.social.models import SocialError
from qq_ai_bot.workspace.store import WorkspaceStore


class ArtifactTransfer:
    def __init__(self, store: WorkspaceStore, root: Path, gateway_root: str) -> None:
        self.store, self.root, self.gateway_root = store, root.absolute(), gateway_root
        self._slots = asyncio.Semaphore(1)

    @asynccontextmanager
    async def prepare(self, artifact_id: str) -> AsyncIterator[tuple[dict[str, Any], str]]:
        async with self._slots:
            async with self._prepare(artifact_id) as result:
                yield result

    @asynccontextmanager
    async def _prepare(self, artifact_id: str) -> AsyncIterator[tuple[dict[str, Any], str]]:
        if not self.gateway_root or not PurePosixPath(self.gateway_root).is_absolute():
            raise SocialError("artifact_transport_unavailable")
        token = str(uuid4())
        path = self.root / token
        try:
            try:
                if self.root.is_symlink() or self.root.resolve() != self.root:
                    raise SocialError("unsafe_transfer_root")
                metadata, data = await asyncio.to_thread(self.store.read_bytes, artifact_id)
                self.root.mkdir(parents=True, exist_ok=True)
                # Only this snapshot crosses the gateway boundary.
                with path.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                path.chmod(0o444)
            except OSError:
                raise SocialError("artifact_transfer_unavailable") from None
            yield metadata, str(PurePosixPath(self.gateway_root) / token)
        finally:
            try:
                if path.exists():
                    path.chmod(0o600)
                    path.unlink()
            except OSError:
                # Do not turn a confirmed network delivery into an apparent failure.
                logging.getLogger(__name__).warning("social_transfer_cleanup_failed")
