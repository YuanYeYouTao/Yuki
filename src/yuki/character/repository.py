"""Character reads and atomic presentation/ownership changes using original receipts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki import __version__
from yuki.character.db_models import CharacterOwnerGrantModel, CharacterProfileModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.identity.person_identity import (
    current_person_id,
    person_enabled_condition,
    person_family_condition,
)
from yuki.time.formatting import utc_iso


async def is_character_owner(session: AsyncSession, person_id: str) -> bool:
    return bool(
        await session.scalar(
            select(CharacterOwnerGrantModel.person_id)
            .where(
                person_family_condition(CharacterOwnerGrantModel.person_id, person_id),
                person_enabled_condition(CharacterOwnerGrantModel.person_id),
                CharacterOwnerGrantModel.revoked_at.is_(None),
            )
            .limit(1)
        )
    )


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


def profile_view(row: CharacterProfileModel, *, owner: bool) -> dict[str, Any]:
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
        "avatar": avatar,
        "avatar_revision": row.avatar_revision,
        "default_avatar": avatar_view(row.default_avatar_sha256),
        "viewer": {"is_owner": owner},
    }


class CharacterRepository:
    def __init__(self, identity: ClientIdentityService) -> None:
        self.identity = identity
        self.database = identity.database

    async def _profile(self, session: AsyncSession) -> CharacterProfileModel:
        profile = await session.scalar(select(CharacterProfileModel))
        if profile is None:
            raise ClientError("unavailable")
        return profile

    async def meta(self) -> dict[str, Any]:
        async with self.database.sessions() as session:
            row = await self._profile(session)
            return {
                "api_revision": 3,
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
                "min_client": {"desktop": "1.0.0-beta.1", "android": None},
            }

    async def get(self, principal: ClientPrincipal | None = None) -> dict[str, Any]:
        async with self.database.sessions() as session:
            owner = False
            if principal is not None:
                principal = await self.identity.require_principal(session, principal)
                owner = principal.is_owner
            return profile_view(await self._profile(session), owner=owner)

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
                await self.identity.require_principal(session, principal) if principal else None
            )
            if current is not None and not current.is_owner:
                raise ClientError("owner_required")
            issuer = current.device_id if current else operator_id
            assert issuer is not None
            row = await self._profile(session)
            operation = "client.character_update"
            bound = fingerprint(
                operation,
                row.id,
                {
                    "expected_revision": expected_revision,
                    **changes,
                },
            )
            old = await self.identity.replay(session, issuer, request_id, bound, principal=current)
            if old is not None:
                return old
            if row.revision != expected_revision:
                raise ClientError(
                    "version_conflict",
                    details={
                        "current": profile_view(
                            row,
                            owner=current.is_owner if current else False,
                        )
                    },
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
            return result

    async def owners(self) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = await session.scalars(
                select(CharacterOwnerGrantModel).order_by(
                    CharacterOwnerGrantModel.granted_at,
                    CharacterOwnerGrantModel.person_id,
                )
            )
            return [
                {
                    "person_id": row.person_id,
                    "granted_by_principal": row.granted_by_principal,
                    "granted_at": utc_iso(row.granted_at),
                    "revoked_at": utc_iso(row.revoked_at),
                }
                for row in rows
            ]

    async def set_owner(
        self,
        operator_id: str,
        person_id: str,
        request_id: str,
        *,
        granted: bool,
    ) -> dict[str, Any]:
        operation = "client.character_owner_grant" if granted else "client.character_owner_revoke"
        bound = fingerprint(operation, person_id, {})
        async with self.database.immediate_session() as session:
            old = await self.identity.replay(session, operator_id, request_id, bound)
            if old is not None:
                return old
            if granted:
                await self.identity._person(session, person_id)
            root = await current_person_id(session, person_id)
            now = datetime.now(UTC)
            family = list(
                await session.scalars(
                    select(CharacterOwnerGrantModel).where(
                        person_family_condition(CharacterOwnerGrantModel.person_id, root),
                    )
                )
            )
            if granted:
                grant = next((row for row in family if row.person_id == root), None)
                if grant is None:
                    session.add(
                        CharacterOwnerGrantModel(
                            person_id=root,
                            granted_by_principal=operator_id,
                            granted_at=now,
                        )
                    )
                else:
                    grant.granted_by_principal, grant.granted_at, grant.revoked_at = (
                        operator_id,
                        now,
                        None,
                    )
            else:
                for grant in family:
                    if grant.revoked_at is None:
                        grant.revoked_at = now
            result: dict[str, Any] = {"person_id": root, "granted": granted}
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=operator_id,
                principal_kind="control",
                person_id=root,
                request_id=request_id,
                operation=operation,
                operation_ref=root,
                payload_hash=bound,
                resource_id=root,
                result=result,
            )
            return result
