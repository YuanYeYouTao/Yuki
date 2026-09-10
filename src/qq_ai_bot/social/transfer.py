"""Short-lived, read-only gateway handoff, separate from the shared workspace."""

from __future__ import annotations

import asyncio
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

    @asynccontextmanager
    async def prepare(self, artifact_id: str) -> AsyncIterator[tuple[dict[str, Any], str]]:
        if not self.gateway_root or not PurePosixPath(self.gateway_root).is_absolute():
            raise SocialError("artifact_transport_unavailable")
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise SocialError("unsafe_transfer_root")
        metadata, data = await asyncio.to_thread(self.store.read_bytes, artifact_id)
        self.root.mkdir(parents=True, exist_ok=True)
        token = str(uuid4())
        path = self.root / token
        try:
            # Random names; the gateway receives only this immutable snapshot, not a workspace path.
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o444)
            yield metadata, str(PurePosixPath(self.gateway_root) / token)
        finally:
            if path.exists():
                path.chmod(0o600)
                path.unlink()
