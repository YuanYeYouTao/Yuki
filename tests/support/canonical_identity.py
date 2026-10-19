"""Explicit QQ seed factories for isolated tests, never runtime identity APIs."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
    external_id,
    find_space_binding,
    trip,
)


async def ensure_person(
    session: AsyncSession,
    user_id: str,
    *,
    display_name: str = "",
    now: datetime | None = None,
) -> str:
    binding = await create_person_binding(
        session,
        external_account_id=external_id(user_id),
        display_name=display_name,
        now=now or datetime.now(UTC),
    )
    trip("after_identity_foundation")
    return binding.person_id


async def ensure_presence(
    session: AsyncSession,
    bot_user_id: str,
    *,
    now: datetime | None = None,
) -> str:
    presence = await create_presence(
        session, external_account_id=external_id(bot_user_id), now=now or datetime.now(UTC)
    )
    trip("after_identity_foundation")
    return presence.id


async def ensure_space(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    enabled: bool = True,
    autonomous_enabled: bool = True,
    require_mention: bool = True,
    now: datetime | None = None,
) -> str:
    space_external_id = external_id(group_id)
    binding = await find_space_binding(session, space_external_id)
    if binding is None:
        binding = await create_space_binding(
            session,
            external_space_id=space_external_id,
            name=name,
            enabled=enabled,
            autonomous_enabled=autonomous_enabled,
            require_mention=require_mention,
            now=now or datetime.now(UTC),
        )
        trip("after_identity_foundation")
    return binding.space_id
