"""Account, Assignment, Person and Space persistence using their internal IDs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final, Literal, cast
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
)
from yuki.identity.db_models import (
    AccountAssignmentModel,
    AccountModel,
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import person_family_ids, same_person_id

IDENTITY_PLATFORM: Final[str] = "qq"
AccountRole = Literal["human", "yuki_self", "external_bot", "private_peer"]
Failpoint = Callable[[str], None]

_failpoint: Failpoint | None = None


def set_identity_failpoint(failpoint: Failpoint | None) -> None:
    """Install deterministic failure injection used by transaction tests."""

    global _failpoint
    _failpoint = failpoint


def trip(name: str) -> None:
    if _failpoint is not None:
        _failpoint(name)


def new_identity_id() -> str:
    return str(uuid4())


def external_id(raw: str) -> str:
    normalized = optional_external_id(raw)
    if normalized is None:
        raise CanonicalIdentityError("unclassified")
    return normalized


def optional_external_id(raw: object | None) -> str | None:
    if raw is None:
        return None
    normalized = str(raw).strip()
    if not normalized or len(normalized) > 255:
        return None
    return normalized


async def find_account(
    session: AsyncSession, external_account_id: str, *, platform: str = IDENTITY_PLATFORM
) -> AccountModel | None:
    return cast(
        AccountModel | None,
        await session.scalar(
            select(AccountModel).where(
                AccountModel.platform == platform,
                AccountModel.external_account_id == external_account_id,
            )
        ),
    )


async def find_assignment(
    session: AsyncSession, external_account_id: str, *, platform: str = IDENTITY_PLATFORM
) -> AccountAssignmentModel | None:
    return cast(
        AccountAssignmentModel | None,
        await session.scalar(
            select(AccountAssignmentModel)
            .join(AccountAssignmentModel.account)
            .options(contains_eager(AccountAssignmentModel.account))
            .where(
                AccountModel.platform == platform,
                AccountModel.external_account_id == external_account_id,
                AccountAssignmentModel.status != "ended",
            )
        ),
    )


async def find_identity_binding(
    session: AsyncSession, external_account_id: str, *, platform: str = IDENTITY_PLATFORM
) -> IdentityBindingModel | None:
    row = await find_assignment(session, external_account_id, platform=platform)
    return row if isinstance(row, IdentityBindingModel) else None


async def find_presence(
    session: AsyncSession, external_account_id: str, *, platform: str = IDENTITY_PLATFORM
) -> PresenceModel | None:
    row = await find_assignment(session, external_account_id, platform=platform)
    return row if isinstance(row, PresenceModel) else None


async def ensure_account(
    session: AsyncSession,
    external_account_id: str,
    *,
    platform: str,
    now: datetime,
    display_name: str = "",
) -> AccountModel:
    account = await find_account(session, external_account_id, platform=platform)
    if account is None:
        account = AccountModel(
            id=new_identity_id(),
            platform=platform,
            external_account_id=external_account_id,
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        session.add(account)
    return account


async def replace_assignment(
    session: AsyncSession,
    account: AccountModel,
    expected_assignment_id: str | None,
    new_owner: CanonicalPersonModel | Literal["yuki"] | None,
    *,
    now: datetime,
) -> AccountAssignmentModel | None:
    """Replace one ownership relation atomically; a pause is never an unbind."""
    with session.no_autoflush:
        current = await find_assignment(
            session, account.external_account_id, platform=account.platform
        )
        if (current.id if current is not None else None) != expected_assignment_id:
            raise CanonicalIdentityError("assignment_changed")
        if current is not None:
            if new_owner == "yuki" and current.kind == "yuki":
                return current
            if isinstance(new_owner, CanonicalPersonModel) and current.person_id is not None:
                if await same_person_id(session, current.person_id, new_owner.id):
                    return current
    if current is not None:
        current.status = "ended"
        current.ended_at = now
        current.updated_at = now
        current.revision += 1
        await session.flush()
    if new_owner is None:
        return None
    cls = PresenceModel if new_owner == "yuki" else IdentityBindingModel
    result = cls(
        id=new_identity_id(),
        account=account,
        person_id=new_owner.id if isinstance(new_owner, CanonicalPersonModel) else None,
        status="active",
        started_at=now,
        ended_at=None,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
        revision=1,
    )
    session.add(result)
    await session.flush()
    return result


def set_assignment_usage(
    assignment: AccountAssignmentModel, enabled: bool, *, now: datetime
) -> None:
    if assignment.status == "ended":
        raise CanonicalIdentityError("assignment_ended")
    status = "active" if enabled else "disabled"
    if assignment.status != status:
        assignment.status = status
        assignment.updated_at = now
        assignment.revision += 1


async def find_space_binding(
    session: AsyncSession,
    external_space_id: str,
    *,
    platform: str = IDENTITY_PLATFORM,
) -> SpaceBindingModel | None:
    return cast(
        SpaceBindingModel | None,
        await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == platform,
                SpaceBindingModel.external_space_id == external_space_id,
            )
        ),
    )


async def bindings_for_person(
    session: AsyncSession,
    person_id: str,
) -> tuple[IdentityBindingModel, ...]:
    rows = list(
        await session.scalars(
            select(IdentityBindingModel)
            .where(IdentityBindingModel.person_id == person_id)
            .order_by(IdentityBindingModel.external_account_id, IdentityBindingModel.id)
        )
    )
    return tuple(rows)


async def create_person_binding(
    session: AsyncSession,
    *,
    external_account_id: str,
    display_name: str,
    now: datetime,
    platform: str = IDENTITY_PLATFORM,
) -> IdentityBindingModel:
    current = await find_assignment(session, external_account_id, platform=platform)
    if current is not None:
        if isinstance(current, IdentityBindingModel):
            return current
        raise CanonicalIdentityError("canonical_kind_mismatch")
    account = await ensure_account(
        session, external_account_id, platform=platform, now=now, display_name=display_name
    )
    person = CanonicalPersonModel(
        id=new_identity_id(),
        display_name=display_name,
        enabled=True,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    session.add(person)
    return cast(
        IdentityBindingModel, await replace_assignment(session, account, None, person, now=now)
    )


async def create_presence(
    session: AsyncSession,
    *,
    external_account_id: str,
    now: datetime,
    platform: str = IDENTITY_PLATFORM,
) -> PresenceModel:
    current = await find_assignment(session, external_account_id, platform=platform)
    if current is not None:
        if isinstance(current, PresenceModel):
            return current
        raise CanonicalIdentityError("canonical_kind_mismatch")
    account = await find_account(session, external_account_id, platform=platform)
    if (
        account is not None
        and await session.scalar(
            select(AccountAssignmentModel.id)
            .where(AccountAssignmentModel.account_id == account.id)
            .limit(1)
        )
        is not None
    ):
        raise CanonicalIdentityError("assignment_ended")
    account = await ensure_account(session, external_account_id, platform=platform, now=now)
    return cast(PresenceModel, await replace_assignment(session, account, None, "yuki", now=now))


async def create_space_binding(
    session: AsyncSession,
    *,
    external_space_id: str,
    name: str,
    enabled: bool,
    autonomous_enabled: bool,
    require_mention: bool,
    now: datetime,
    platform: str = IDENTITY_PLATFORM,
) -> SpaceBindingModel:
    try:
        async with session.begin_nested():
            space = CanonicalSpaceModel(
                id=new_identity_id(),
                name=name[:128],
                enabled=enabled,
                autonomous_enabled=autonomous_enabled,
                require_mention=require_mention,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            binding = SpaceBindingModel(
                id=new_identity_id(),
                space_id=space.id,
                platform=platform,
                external_space_id=external_space_id,
                display_name=name[:128],
                status="active",
                revision=1,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add_all((space, binding))
            await session.flush()
            return binding
    except IntegrityError:
        existing = await find_space_binding(
            session,
            external_space_id,
            platform=platform,
        )
        if existing is None:
            raise CanonicalIdentityError("canonical_kind_mismatch") from None
        return existing


def assert_same_shadow(current: str | None, proven: str | None) -> None:
    if proven is not None and current is not None and current != proven:
        raise CanonicalIdentityError("canonical_owner_mismatch")


async def forget_person_by_id(session: AsyncSession, person_id: str) -> None:
    """Delete exactly the selected Person aggregate, including historical bindings."""

    trip("before_forget_canonical")
    family = await person_family_ids(session, person_id)
    conversations = tuple(
        await session.scalars(
            select(CanonicalConversationModel.id).where(
                CanonicalConversationModel.kind == "private",
                CanonicalConversationModel.person_id.in_(family),
            )
        )
    )
    await session.execute(
        delete(PersonActiveRouteModel).where(PersonActiveRouteModel.person_id.in_(family))
    )
    # Delete Conversations before their pinned aliases, whose trigger protects live entries.
    await session.execute(
        delete(CanonicalConversationModel).where(CanonicalConversationModel.id.in_(conversations))
    )
    await session.execute(
        delete(ConversationLegacyAliasModel).where(
            ConversationLegacyAliasModel.conversation_id.in_(conversations)
        )
    )
    await session.execute(
        delete(IdentityBindingModel).where(IdentityBindingModel.person_id.in_(family))
    )
    await session.execute(
        delete(CanonicalPersonModel).where(
            CanonicalPersonModel.id.in_(family),
            CanonicalPersonModel.merged_into_person_id.is_not(None),
        )
    )
    await session.execute(delete(CanonicalPersonModel).where(CanonicalPersonModel.id.in_(family)))


__all__ = [
    "AccountRole",
    "assert_same_shadow",
    "bindings_for_person",
    "create_person_binding",
    "create_presence",
    "create_space_binding",
    "external_id",
    "find_identity_binding",
    "find_presence",
    "find_space_binding",
    "forget_person_by_id",
    "new_identity_id",
    "optional_external_id",
    "set_identity_failpoint",
    "trip",
]
