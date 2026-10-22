"""Paged contact subjects and original entry discovery without transport lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import person_family_condition, person_root_id
from yuki.persistence.models import ChatEventModel, PersonAliasModel
from yuki.services.contact_query import CONTACT_QUERY, EntryContactsQuery
from yuki.services.history_query import HistorySource
from yuki.social.history_sources import history_source_statement
from yuki.social.models import SocialError

if TYPE_CHECKING:
    from yuki.social.service import SocialContext, SocialService


async def subject_contacts(
    session: AsyncSession,
    kind: str,
    query: str = "",
    *,
    exact: bool = False,
    target_id: str | None = None,
    source: HistorySource | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    if source is not None and source.scope is not None:
        if source.scope != ("private" if kind == "person" else "group"):
            return []
    if kind == "person":
        identity, name = CanonicalPersonModel.id, CanonicalPersonModel.display_name
        known = (
            select(ChatEventModel.id)
            .where(
                person_family_condition(ChatEventModel.author_person_id, identity),
                ChatEventModel.direction == "inbound",
                ChatEventModel.author_kind == "person",
            )
            .exists()
        )
        name_match = name == query if exact else name.contains(query, autoescape=True)
        alias_match = (
            select(PersonAliasModel.id)
            .where(
                person_family_condition(PersonAliasModel.canonical_person_id, identity),
                PersonAliasModel.canonical_space_id.is_(None),
                PersonAliasModel.alias == query
                if exact
                else PersonAliasModel.alias.contains(query, autoescape=True),
            )
            .exists()
        )
        statement = select(identity, name).where(
            CanonicalPersonModel.enabled.is_(True),
            CanonicalPersonModel.merged_into_person_id.is_(None),
            known,
            or_(name_match, alias_match) if query else true(),
        )
        if source is not None and source.platform is not None:
            statement = statement.where(
                select(IdentityBindingModel.id)
                .where(
                    person_family_condition(IdentityBindingModel.person_id, identity),
                    IdentityBindingModel.status == "active",
                    IdentityBindingModel.platform == source.platform,
                )
                .exists()
            )
    elif kind == "space":
        identity, name = CanonicalSpaceModel.id, CanonicalSpaceModel.name
        statement = select(identity, name).where(
            CanonicalSpaceModel.enabled.is_(True),
            name == query if exact else name.contains(query, autoescape=True),
        )
        if source is not None and source.platform is not None:
            statement = statement.where(
                select(SpaceBindingModel.id)
                .where(
                    SpaceBindingModel.space_id == identity,
                    SpaceBindingModel.status == "active",
                    SpaceBindingModel.platform == source.platform,
                )
                .exists()
            )
    else:
        raise SocialError("invalid_target_kind")
    if target_id is not None:
        statement = statement.where(identity == target_id)
    if cursor is not None:
        statement = statement.where(identity > cursor)
    statement = statement.order_by(identity)
    if limit is not None:
        statement = statement.limit(limit)
    return [
        {"target_id": row[0], "display_name": row[1], "kind": kind}
        for row in (await session.execute(statement)).all()
    ]


async def read_contacts(
    service: SocialService, arguments: dict[str, Any], context: SocialContext
) -> dict[str, Any]:
    request = CONTACT_QUERY.validate_python({"view": "subjects", **arguments})
    selected = None
    if not isinstance(request, EntryContactsQuery) and (request.target_id or request.subject_ref):
        selected = await service.target(request.kind, arguments, context)
    async with service.database.read_session() as session:
        await session.execute(text("BEGIN"))
        if isinstance(request, EntryContactsQuery):
            statement = await history_source_statement(
                session, context=context, target=request.target, source=request.source
            )
            columns = statement.selected_columns
            if request.display_name:
                statement = statement.where(
                    columns.conversation_name.contains(request.display_name, autoescape=True)
                )
            if request.cursor is not None:
                statement = statement.where(columns.conversation_id > str(request.cursor))
            entries = (
                (
                    await session.execute(
                        statement.order_by(columns.conversation_id).limit(request.limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            items = [
                {
                    "conversation_id": row["conversation_id"],
                    "kind": "person" if row["conversation_kind"] == "private" else "space",
                    "target_id": row["person_id"] or row["space_id"],
                    "person_id": row["person_id"],
                    "space_id": row["space_id"],
                    "binding_id": row["binding_id"],
                    "entry_presence_id": row["presence_id"],
                    "thread_id": row["thread_id"],
                    "platform": row["platform"],
                    "scope": "private" if row["conversation_kind"] == "private" else "group",
                    "display_name": row["conversation_name"],
                    "binding_status": row["binding_status"],
                    "presence_status": row["presence_status"],
                }
                for row in entries[: request.limit]
            ]
            has_more = len(entries) > request.limit
            cursor_key = "conversation_id"
        else:
            rows = await subject_contacts(
                session,
                request.kind,
                request.display_name,
                target_id=str(selected.id) if selected is not None else None,
                source=request.source,
                limit=request.limit + 1,
                cursor=str(request.cursor) if request.cursor is not None else None,
            )
            has_more = len(rows) > request.limit
            items = rows[: request.limit]
            identities = [item["target_id"] for item in items]
            bindings_by_target: dict[str, list[dict[str, str]]] = {
                identity: [] for identity in identities
            }
            if identities:
                model = IdentityBindingModel if request.kind == "person" else SpaceBindingModel
                owner = (
                    person_root_id(IdentityBindingModel.person_id)
                    if request.kind == "person"
                    else SpaceBindingModel.space_id
                )
                statement = select(owner, model.id, model.platform).where(
                    owner.in_(identities), model.status == "active"
                )
                if request.source is not None and request.source.platform is not None:
                    statement = statement.where(model.platform == request.source.platform)
                for owner_id, binding_id, platform in (
                    await session.execute(statement.order_by(model.id))
                ).all():
                    bindings_by_target[owner_id].append(
                        {"binding_id": binding_id, "platform": platform}
                    )
            for item in items:
                item["bindings"] = bindings_by_target[item["target_id"]]
            cursor_key = "target_id"
    page = {
        "items": items,
        "has_more": has_more,
        "next_cursor": items[-1][cursor_key] if has_more else None,
    }
    if selected is not None and items:
        return {**items[0], "has_more": has_more, "next_cursor": page["next_cursor"]}
    return page
