"""Immutable normalized avatar assets, published before atomic profile receipts."""

import errno
import hashlib
import os
import struct
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from PIL import Image, PngImagePlugin, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.character.repository import CharacterRepository, profile_view
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.services.media_resolver import _media_io

SIZES = (1024, 512, 256, 128, 64)


def _sync_directory(path: Path) -> None:
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
            temporary = tempfile.TemporaryDirectory(prefix=".avatar-", dir=self.root)
        except OSError as exc:
            raise ClientError("unavailable") from exc
        try:
            directory = Path(temporary.name)
            source = directory / "upload.png"
            output = source.open("xb")
            digest = hashlib.sha256()
            length = 0
            try:
                async for chunk in chunks:
                    length += len(chunk)
                    if length > size:
                        raise ClientError("validation_error")
                    digest.update(chunk)
                    await _media_io(output.write, chunk)
                await _media_io(output.flush)
                await _media_io(os.fsync, output.fileno())
            finally:
                await _media_io(output.close)
            if length != size or digest.hexdigest() != sha256:
                raise ClientError("validation_error")
            await _media_io(self._publish, directory, source, sha256)
            return sha256
        except OSError as exc:
            raise ClientError("unavailable") from exc
        finally:
            await _media_io(temporary.cleanup)

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
            image.save(path, format="PNG", pnginfo=color)
            with path.open("rb") as stream:
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
    def __init__(self, identity: ClientIdentityService, store: AvatarStore) -> None:
        self.identity, self.store = identity, store
        self.repository = CharacterRepository(identity)

    async def _actor(
        self,
        session: AsyncSession,
        principal: ClientPrincipal | None,
        operator_id: str | None,
    ) -> ClientPrincipal | None:
        if principal is not None:
            principal = await self.identity.require_principal(session, principal)
            if not principal.is_owner:
                raise ClientError("owner_required")
        else:
            assert operator_id is not None
        return principal

    async def change(
        self,
        request_id: str,
        expected_revision: int,
        *,
        action: Literal["set", "reset", "default"],
        sha256: str | None = None,
        size: int | None = None,
        chunks: AsyncIterator[bytes] | None = None,
        principal: ClientPrincipal | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        operation = "client.character_avatar"
        bound = fingerprint(
            operation,
            action,
            {
                "expected_revision": expected_revision,
                "sha256": sha256,
                "size": size,
            },
        )
        async with self.identity.database.sessions() as session:
            current = await self._actor(session, principal, operator_id)
            issuer = current.device_id if current else operator_id
            assert issuer is not None
            previous = await self.identity.replay(
                session, issuer, request_id, bound, principal=current
            )
            if previous is not None:
                return previous
        if action != "reset":
            assert chunks is not None and sha256 is not None and size is not None
            await self.store.store(chunks, sha256, size)
        async with self.identity.database.immediate_session() as session:
            current = await self._actor(session, principal, operator_id)
            previous = await self.identity.replay(
                session, issuer, request_id, bound, principal=current
            )
            if previous is not None:
                return previous
            row = await self.repository._profile(session)
            if row.avatar_revision != expected_revision:
                raise ClientError(
                    "version_conflict", details={"current": profile_view(row, owner=True)}
                )
            if action == "default":
                row.default_avatar_sha256 = sha256
            else:
                row.avatar_sha256 = row.default_avatar_sha256 if action == "reset" else sha256
            row.avatar_revision += 1
            row.updated_at = datetime.now(UTC)
            result = profile_view(row, owner=current.is_owner if current else False)
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=issuer,
                principal_kind="device" if current else "control",
                person_id=current.person_id if current else issuer,
                request_id=request_id,
                operation=operation,
                operation_ref=row.id,
                payload_hash=bound,
                resource_id=row.id,
                result=result,
                request_person_id=current.request_person_id if current else None,
                request_conversation_id=current.request_conversation_id if current else None,
            )
            return {**result, "replay": False}

    async def file(self, sha256: str, size: int, principal: ClientPrincipal | None) -> Path:
        async with self.identity.database.sessions() as session:
            if principal is not None:
                await self.identity.require_principal(session, principal)
            else:
                profile = await self.repository._profile(session)
                if size != 128 or profile.avatar_sha256 != sha256:
                    raise ClientError("unauthenticated")
        path = self.store.path(sha256, size)
        if not await _media_io(path.is_file):
            raise ClientError("not_found")
        return path
