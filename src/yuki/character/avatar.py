"""Immutable normalized avatar assets, published before atomic profile receipts."""

import errno
import hashlib
import os
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from PIL import Image, PngImagePlugin, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.character.repository import CharacterRepository, profile_view
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientPrincipal, fingerprint
from yuki.services.media_resolver import _media_directory, _media_io

SIZES = (1024, 512, 256, 128, 64)


def _sync_directory(path: Path) -> None:
    # Variants are individually flushed; Windows does not expose directory fsync.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AvatarStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, sha256: str, size: int) -> Path:
        if (
            len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or size not in SIZES
        ):
            raise ClientError("not_found")
        return self.root / sha256 / f"{size}.png"

    async def store(self, chunks: AsyncIterator[bytes], sha256: str, size: int) -> str:
        if size < 0 or len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ClientError("validation_error")
        try:
            await _media_io(self._ensure_root)
            async with _media_directory(".avatar-", dir=self.root) as directory:
                source = directory / "upload.png"
                output = None

                def open_source() -> None:
                    nonlocal output
                    output = source.open("xb")

                digest = hashlib.sha256()
                length = 0
                try:
                    await _media_io(open_source)
                    assert output is not None
                    async for chunk in chunks:
                        length += len(chunk)
                        if length > size:
                            raise ClientError("validation_error")
                        digest.update(chunk)
                        await _media_io(output.write, chunk)
                    await _media_io(output.flush)
                    await _media_io(os.fsync, output.fileno())
                finally:
                    if output is not None:
                        await _media_io(output.close)
                if length != size or digest.hexdigest() != sha256:
                    raise ClientError("validation_error")
                await _media_io(self._publish, directory, source, sha256)
                return sha256
        except OSError as exc:
            raise ClientError("unavailable") from exc

    def _ensure_root(self) -> None:
        missing = []
        current = self.root
        while not current.exists():
            missing.append(current)
            current = current.parent
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in reversed(missing):
            _sync_directory(directory.parent)

    def _publish(self, directory: Path, source: Path, sha256: str) -> None:
        with source.open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise ClientError("unsupported_image")
        if struct.unpack(">II", header[16:24]) != (1024, 1024):
            raise ClientError("image_dimensions")
        try:
            with Image.open(source) as candidate:
                candidate.verify()
            with Image.open(source) as candidate:
                candidate.load()
                normalized = Image.new("RGBA", (1024, 1024))
                normalized.paste(candidate.convert("RGBA"))
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, MemoryError) as exc:
            raise ClientError("unsupported_image") from exc

        color = PngImagePlugin.PngInfo()
        color.add(b"sRGB", b"\x00")
        for size in SIZES:
            image = (
                normalized
                if size == 1024
                else normalized.resize(
                    (size, size),
                    Image.Resampling.LANCZOS,
                )
            )
            path = directory / f"{size}.png"
            with path.open("xb") as stream:
                image.save(stream, format="PNG", pnginfo=color)
                stream.flush()
                os.fsync(stream.fileno())
        source.unlink()
        _sync_directory(directory)
        try:
            directory.rename(self.root / sha256)
        except OSError as exc:
            # An immutable directory is visible only after its entire publication.
            # Two same-content uploads can finish in either order.
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
        _sync_directory(self.root)


class CharacterAvatars:
    def __init__(self, repository: CharacterRepository, store: AvatarStore) -> None:
        self.repository, self.store = repository, store
        self.identity = repository.identity

    async def change(
        self,
        request_id: str,
        expected_revision: int,
        *,
        action: Literal["set", "reset"],
        sha256: str | None = None,
        size: int | None = None,
        chunks: AsyncIterator[bytes] | None = None,
        principal: ClientPrincipal,
    ) -> dict[str, Any]:
        operation = "client.character_avatar"
        async with self.identity.database.read_session() as session:
            current = await self.identity.require_superuser(session, principal)
            row = await self.repository._profile(session)
            bound = fingerprint(
                operation,
                row.id,
                {
                    "action": action,
                    "expected_revision": expected_revision,
                    "sha256": sha256,
                    "size": size,
                },
            )
            issuer = current.device_id
            previous = await self.identity.replay(
                session, issuer, request_id, bound, principal=current
            )
            if previous is not None:
                return previous
        if action != "reset":
            assert chunks is not None and sha256 is not None and size is not None
            await self.store.store(chunks, sha256, size)
        async with self.identity.database.immediate_session() as session:
            current = await self.identity.require_superuser(session, principal)
            previous = await self.identity.replay(
                session, issuer, request_id, bound, principal=current
            )
            if previous is not None:
                return previous
            row = await self.repository._profile(session)
            if row.avatar_revision != expected_revision:
                raise ClientError("version_conflict", details={"current": profile_view(row)})
            row.avatar_sha256 = row.default_avatar_sha256 if action == "reset" else sha256
            row.avatar_revision += 1
            row.updated_at = datetime.now(UTC)
            result = profile_view(row)
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=issuer,
                principal_kind="device",
                person_id=current.person_id,
                request_id=request_id,
                operation=operation,
                operation_ref=row.id,
                payload_hash=bound,
                resource_id=row.id,
                result=result,
                request_person_id=current.request_person_id,
                request_conversation_id=current.request_conversation_id,
            )
            return {**result, "replay": False}

    async def _authorize_read(
        self, session: AsyncSession, sha256: str, size: int, principal: ClientPrincipal | None
    ) -> None:
        if principal is not None:
            await self.identity.require_principal(session, principal)
        else:
            profile = await self.repository._profile(session)
            if size != 128 or profile.avatar_sha256 != sha256:
                raise ClientError("unauthenticated")

    async def read(self, sha256: str, size: int, principal: ClientPrincipal | None) -> bytes:
        async with self.identity.database.read_session() as session:
            await self._authorize_read(session, sha256, size, principal)
        path = self.store.path(sha256, size)
        try:
            content = await _media_io(path.read_bytes)
        except OSError as exc:
            raise ClientError("not_found") from exc
        async with self.identity.database.read_session() as session:
            await self._authorize_read(session, sha256, size, principal)
        return content
