"""Original account-stage authors, shared by ledger writers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.domain.identity import AuthorKind
from yuki.identity.canonical_repository import IDENTITY_PLATFORM, external_id
from yuki.identity.db_models import AccountModel, CanonicalPersonModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.ownership_interval import assignments_at
from yuki.identity.write_settings import identity_write_settings


@dataclass(frozen=True, slots=True)
class EventAuthorProjection:
    author_kind: str
    author_person_id: str | None
    author_presence_id: str | None
    author_account_id: str | None = None
    assignment_id: str | None = None

    def as_tuple(self) -> tuple[str, str | None, str | None]:
        return self.author_kind, self.author_person_id, self.author_presence_id


async def project_event_author(
    session: AsyncSession,
    *,
    sender_user_id: str,
    sender_is_bot: bool = False,
    event_kind: str = "message",
    direction: str = "inbound",
    platform: str = IDENTITY_PLATFORM,
    occurred_at: datetime | None = None,
) -> EventAuthorProjection:
    if event_kind == "external_event" or direction == "external":
        return EventAuthorProjection(AuthorKind.SYSTEM.value, None, None)
    sender_id = external_id(sender_user_id)
    return (
        await project_event_authors(
            session,
            sender_user_ids=(sender_id,),
            sender_is_bot=sender_is_bot,
            platform=platform,
            occurred_at=occurred_at,
        )
    )[sender_id]


async def project_event_authors(
    session: AsyncSession,
    *,
    sender_user_ids: tuple[str, ...],
    sender_is_bot: bool = False,
    platform: str = IDENTITY_PLATFORM,
    occurred_at: datetime | None = None,
) -> dict[str, EventAuthorProjection]:
    native_ids = tuple(dict.fromkeys(external_id(item) for item in sender_user_ids))
    if not native_ids:
        return {}
    accounts = {
        row.external_account_id: row
        for row in await session.scalars(
            select(AccountModel).where(
                AccountModel.platform == platform, AccountModel.external_account_id.in_(native_ids)
            )
        )
    }
    stages = await assignments_at(session, tuple(row.id for row in accounts.values()), occurred_at)
    person_ids = {row.person_id for row in stages.values() if row is not None and row.person_id}
    raw, root = aliased(CanonicalPersonModel), aliased(CanonicalPersonModel)
    enabled: dict[str, bool] = (
        dict(
            (
                await session.execute(
                    select(raw.id, raw.enabled & root.enabled)
                    .join(root, root.id == func.coalesce(raw.merged_into_person_id, raw.id))
                    .where(raw.id.in_(person_ids))
                )
            )
            .tuples()
            .all()
        )
        if person_ids
        else {}
    )
    ignored = identity_write_settings().ignored_bot_users if platform == "qq" else ()
    result: dict[str, EventAuthorProjection] = {}
    for native in native_ids:
        account = accounts.get(native)
        stage = stages.get(account.id) if account is not None else None
        if account is not None and account.id in stages and stage is None:
            author = EventAuthorProjection(AuthorKind.ACCOUNT.value, None, None, account.id)
        elif stage is not None and stage.kind == "yuki":
            author = EventAuthorProjection(
                AuthorKind.YUKI.value, None, stage.id, assignment_id=stage.id
            )
        elif sender_is_bot or native in ignored:
            author = EventAuthorProjection(AuthorKind.EXTERNAL_BOT.value, None, None)
        elif stage is not None:
            person_id = cast(str, stage.person_id)
            if stage.status != "ended" and (stage.status != "active" or not enabled.get(person_id)):
                raise CanonicalIdentityError("canonical_owner_disabled")
            author = EventAuthorProjection(
                AuthorKind.PERSON.value, person_id, None, assignment_id=stage.id
            )
        else:
            author = EventAuthorProjection(AuthorKind.PERSON.value, None, None)
        result[native] = author
    return result
