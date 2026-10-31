"""Character presentation changes keep the original identity and request receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki import __version__
from yuki.character.db_models import CharacterProfileModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.version import CLIENT_API_REVISION
from yuki.time.formatting import utc_iso

if TYPE_CHECKING:
    from yuki.config import Settings


@dataclass(frozen=True, slots=True)
class CharacterCurrent:
    id: str
    display_name: str
    revision: int
    avatar_revision: int


def avatar_view(sha256: str | None) -> dict[str, Any] | None:
    if sha256 is None:
        return None
    return {
        "sha256": sha256,
        "sizes": {
            str(size): f"/client/v1/character/avatar/{sha256}/{size}.png"
            for size in (64, 128, 256, 512)
        },
    }


def profile_view(row: CharacterProfileModel) -> dict[str, Any]:
    avatar = avatar_view(row.avatar_sha256)
    if avatar is not None:
        avatar.update(revision=row.avatar_revision, updated_at=utc_iso(row.updated_at))
    return {
        "id": row.id,
        "slug": row.slug,
        "display_name": row.display_name,
        "bio": row.bio,
        "accent": {"light": row.accent_light, "dark": row.accent_dark},
        "revision": row.revision,
        "avatar_revision": row.avatar_revision,
        "avatar": avatar,
        "default_avatar": avatar_view(row.default_avatar_sha256),
    }


class CharacterRepository:
    def __init__(self, identity: ClientIdentityService, settings: Settings) -> None:
        self.identity, self.settings = identity, settings
        self.database = identity.database

    async def _profile(self, session: AsyncSession) -> CharacterProfileModel:
        row = await session.scalar(select(CharacterProfileModel))
        if row is None:
            raise ClientError("unavailable")
        return row

    async def current(self, session: AsyncSession | None = None) -> CharacterCurrent:
        if session is None:
            async with self.database.read_session() as reader:
                return await self.current(reader)
        row = await self._profile(session)
        return CharacterCurrent(row.id, row.display_name, row.revision, row.avatar_revision)

    async def initialize(self, display_name: str, *, slug: str | None = None) -> CharacterCurrent:
        """Explicit setup only; an existing identity is never renamed by initialization."""
        async with self.database.immediate_session() as session:
            row = await session.scalar(select(CharacterProfileModel))
            if row is None:
                now, identity = datetime.now(UTC), str(uuid4())
                row = CharacterProfileModel(
                    id=identity,
                    singleton=1,
                    slug=slug or identity,
                    display_name=display_name,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
            result = CharacterCurrent(row.id, row.display_name, row.revision, row.avatar_revision)
        self.settings.apply_character_name(result.display_name, result.revision)
        return result

    async def meta(self) -> dict[str, Any]:
        async with self.database.read_session() as session:
            row = await self._profile(session)
            return {
                "api_revision": CLIENT_API_REVISION,
                "server_version": __version__,
                "character": {
                    "id": row.id,
                    "display_name": row.display_name,
                    "accent": {"light": row.accent_light, "dark": row.accent_dark},
                    "avatar_url": (
                        f"/client/v1/character/avatar/{row.avatar_sha256}/128.png"
                        if row.avatar_sha256
                        else None
                    ),
                },
                "min_client": {"desktop": None, "android": None},
                "capabilities": [
                    "character",
                    "soul",
                    "character_avatar",
                    "memory",
                    "automations",
                    "capabilities",
                    "proactive_route",
                    "artifact_catalog",
                    "work_catalog",
                    "work_steps",
                    "character_status",
                    "computer",
                ],
            }

    async def get(self, principal: ClientPrincipal | None = None) -> dict[str, Any]:
        async with self.database.read_session() as session:
            if principal is not None:
                await self.identity.require_principal(session, principal)
            return profile_view(await self._profile(session))

    async def update(
        self,
        request_id: str,
        expected_revision: int,
        changes: dict[str, Any],
        *,
        principal: ClientPrincipal | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            current = (
                await self.identity.require_superuser(session, principal) if principal else None
            )
            issuer = current.device_id if current else operator_id
            assert issuer is not None
            row = await self._profile(session)
            bound = fingerprint(
                "client.character_update",
                row.id,
                {"expected_revision": expected_revision, **changes},
            )
            old = await self.identity.replay(session, issuer, request_id, bound, principal=current)
            if old is not None:
                result = old
            else:
                if row.revision != expected_revision:
                    raise ClientError(
                        "version_conflict",
                        details={"current": profile_view(row)},
                    )
                for key in ("display_name", "bio", "slug"):
                    if key in changes:
                        setattr(row, key, changes[key])
                if "accent" in changes:
                    row.accent_light, row.accent_dark = (
                        changes["accent"]["light"],
                        changes["accent"]["dark"],
                    )
                row.revision += 1
                row.updated_at = datetime.now(UTC)
                result = profile_view(row)
                await self.identity.receipts.record_personal_command(
                    session,
                    principal_id=issuer,
                    principal_kind="device" if current else "control",
                    person_id=current.person_id if current else issuer,
                    request_id=request_id,
                    operation="client.character_update",
                    operation_ref=row.id,
                    payload_hash=bound,
                    resource_id=row.id,
                    result=result,
                    request_person_id=current.request_person_id if current else None,
                    request_conversation_id=current.request_conversation_id if current else None,
                )
            active_name, active_revision = row.display_name, row.revision
        self.settings.apply_character_name(active_name, active_revision)
        return result
