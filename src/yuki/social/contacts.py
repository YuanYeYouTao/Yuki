"""Paged contact subjects and original entry discovery without transport lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import current_person_id, person_family_condition
from yuki.persistence.models import ChatEventModel, PersonAliasModel
from yuki.services.contact_query import ContactsQuery
from yuki.services.history_query import HistorySource, HistoryTarget
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


async def contact_subject(session: AsyncSession, target: HistoryTarget) -> tuple[str, str]:
    """The Person or Space a single internal selector points at."""
    if target.person_id is not None:
        return "person", str(target.person_id)
    if target.space_id is not None:
        return "space", str(target.space_id)
    if target.conversation_id is not None:
        conversation = await session.get(CanonicalConversationModel, str(target.conversation_id))
        if conversation is not None and (conversation.space_id or conversation.person_id):
            if conversation.space_id:
                return "space", conversation.space_id
            return "person", cast(str, conversation.person_id)
    if target.binding_id is not None:
        personal = await session.get(IdentityBindingModel, str(target.binding_id))
        if personal is not None and personal.person_id:
            return "person", personal.person_id
        group = await session.get(SpaceBindingModel, str(target.binding_id))
        if group is not None:
            return "space", group.space_id
    raise SocialError("target_not_found")


async def read_contacts(
    service: SocialService, arguments: dict[str, Any], context: SocialContext
) -> dict[str, Any]:
    """Subjects with their current entries and original conversations, paged by subject.

    An explicit target is read as itself: historic conversations stay discoverable
    without the name search's current-contact filter, as entry discovery always was.
    """
    request = ContactsQuery.model_validate(arguments)
    source = HistorySource(platform=request.platform) if request.platform else None
    async with service.database.read_session() as session:
        await session.execute(text("BEGIN"))
        has_more = False
        if request.target is not None:
            kind, raw = await contact_subject(session, request.target)
            if kind == "person":
                raw = await current_person_id(session, raw)
                subject = await session.get(CanonicalPersonModel, raw)
                name = subject.display_name if subject is not None else None
            else:
                space = await session.get(CanonicalSpaceModel, raw)
                name = space.name if space is not None else None
            label = "person" if kind == "person" else "group"
            rows = (
                [{"kind": kind, "target_id": raw, "display_name": name}]
                if request.type in (None, label)
                else []
            )
        else:
            after_kind, after_id = (
                request.cursor.split(":", 1) if request.cursor is not None else (None, None)
            )
            kinds = [
                kind
                for kind, label in (("person", "person"), ("space", "group"))
                if request.type in (None, label) and (after_kind != "group" or label == "group")
            ]
            rows = []
            for kind in kinds:
                label = "person" if kind == "person" else "group"
                rows.extend(
                    await subject_contacts(
                        session,
                        kind,
                        request.name,
                        source=source,
                        limit=request.limit + 1 - len(rows),
                        cursor=after_id if after_kind == label else None,
                    )
                )
                if len(rows) > request.limit:
                    break
            has_more = len(rows) > request.limit
        items = []
        for row in rows[: request.limit]:
            kind = str(row["kind"])
            model = IdentityBindingModel if kind == "person" else SpaceBindingModel
            owner = (
                person_family_condition(IdentityBindingModel.person_id, row["target_id"])
                if kind == "person"
                else SpaceBindingModel.space_id == row["target_id"]
            )
            entries = select(model.id, model.platform).where(owner, model.status == "active")
            if request.platform is not None:
                entries = entries.where(model.platform == request.platform)
            statement = await history_source_statement(
                session,
                context=context,
                target=HistoryTarget.model_validate(
                    {"person_id" if kind == "person" else "space_id": row["target_id"]}
                ),
                source=source,
            )
            columns = statement.selected_columns
            conversations = (
                (await session.execute(statement.order_by(columns.conversation_id)))
                .mappings()
                .all()
            )
            item = {
                "type": "person" if kind == "person" else "group",
                "id": row["target_id"],
                "name": row["display_name"],
                "entries": [
                    {"binding_id": binding_id, "platform": platform}
                    for binding_id, platform in (
                        await session.execute(entries.order_by(model.id))
                    ).all()
                ],
                "conversations": [
                    {
                        key: value
                        for key, value in {
                            "conversation_id": entry["conversation_id"],
                            "platform": entry["platform"],
                            "scope": "private"
                            if entry["conversation_kind"] == "private"
                            else "group",
                            "name": entry["conversation_name"],
                            "thread_id": entry["thread_id"],
                            "person_id": entry["person_id"]
                            if entry["person_id"] != row["target_id"]
                            else None,
                            # Only an entry that is no longer current says so.
                            "binding_status": entry["binding_status"]
                            if entry["binding_status"] != "active"
                            else None,
                            "presence_status": entry["presence_status"]
                            if entry["presence_status"] != "active"
                            else None,
                        }.items()
                        if value
                    }
                    for entry in conversations
                ],
            }
            items.append({key: value for key, value in item.items() if value is not None})
    page: dict[str, Any] = {"items": items}
    if has_more:
        page["next_cursor"] = f"{items[-1]['type']}:{items[-1]['id']}"
    return page
