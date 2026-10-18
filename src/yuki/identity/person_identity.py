"""Current human ownership without rewriting original identity or request proofs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import ScalarSelect

from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.errors import CanonicalIdentityError


def person_root_id(value: Any) -> ScalarSelect[str]:
    person = aliased(CanonicalPersonModel)
    return (
        select(func.coalesce(person.merged_into_person_id, person.id))
        .where(person.id == value)
        .correlate_except(person)
        .scalar_subquery()
    )


def same_person_condition(left: Any, right: Any) -> ColumnElement[bool]:
    return person_root_id(left) == person_root_id(right)


def person_enabled_condition(value: Any) -> ColumnElement[bool]:
    """Current human enabled, without reviving a disabled original source."""
    original, root = aliased(CanonicalPersonModel), aliased(CanonicalPersonModel)
    return (
        select(original.id)
        .join(root, root.id == func.coalesce(original.merged_into_person_id, original.id))
        .where(original.id == value, original.enabled.is_(True), root.enabled.is_(True))
        .correlate_except(original, root)
        .exists()
    )


def person_family_condition(column: Any, person_id: Any) -> ColumnElement[bool]:
    person = aliased(CanonicalPersonModel)
    root = person_root_id(person_id)
    return cast(
        ColumnElement[bool],
        column.in_(
            select(person.id).where(or_(person.id == root, person.merged_into_person_id == root))
        ),
    )


async def current_person_id(session: AsyncSession, person_id: str) -> str:
    value = await session.scalar(select(person_root_id(person_id)))
    if value is None:
        raise CanonicalIdentityError("missing_canonical_owner")
    return str(value)


async def person_family_ids(session: AsyncSession, person_id: str) -> tuple[str, ...]:
    root = await current_person_id(session, person_id)
    return tuple(
        await session.scalars(
            select(CanonicalPersonModel.id)
            .where(
                or_(
                    CanonicalPersonModel.id == root,
                    CanonicalPersonModel.merged_into_person_id == root,
                )
            )
            .order_by(CanonicalPersonModel.id)
        )
    )


async def same_person_id(session: AsyncSession, left: str, right: str) -> bool:
    return bool(await session.scalar(select(same_person_condition(left, right))))


async def current_person_ids(session: AsyncSession, person_ids: Iterable[str]) -> dict[str, str]:
    person = CanonicalPersonModel
    return dict(
        (
            await session.execute(
                select(person.id, func.coalesce(person.merged_into_person_id, person.id)).where(
                    person.id.in_(tuple(dict.fromkeys(person_ids)))
                )
            )
        )
        .tuples()
        .all()
    )


async def rename_person(
    session: AsyncSession, person_id: str, display_name: str, *, now: datetime
) -> CanonicalPersonModel:
    """Change the current Person name; account and event names are independent."""
    from yuki.persistence.models import PersonAliasModel

    person = await session.get(CanonicalPersonModel, await current_person_id(session, person_id))
    assert person is not None
    name = display_name.strip()[:128]
    old_alias = (
        await session.scalar(
            select(PersonAliasModel).where(
                PersonAliasModel.canonical_person_id == person.id,
                PersonAliasModel.canonical_space_id.is_(None),
                PersonAliasModel.alias == person.display_name,
            )
        )
        if person.display_name and person.display_name != name
        else None
    )
    if person.display_name == name:
        return person
    if person.display_name and old_alias is None:
        session.add(
            PersonAliasModel(
                canonical_person_id=person.id,
                canonical_space_id=None,
                alias=person.display_name,
                alias_type="name",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    person.display_name = name
    person.revision += 1
    person.updated_at = now
    return person


async def merge_persons(
    session: AsyncSession,
    source_person_id: str,
    target_person_id: str,
    *,
    now: datetime | None = None,
    actor_id: str = "system",
) -> str:
    """Associate the same human, retaining original owners and every entry.

    The caller owns a consistent read transaction and its short write/commit.
    Only currently effective unique configuration rows move. Conflicting source
    rows remain historical and never become fallback values after an unset.
    """
    from yuki.conversation.canonical_db_models import PersonActiveRouteModel
    from yuki.persistence.models import (
        AdminOperationEventModel,
        PersonAliasModel,
        RuntimeConfigOverrideModel,
    )
    from yuki.plugin_host.db_models import PluginConfigValueModel

    source_id = await current_person_id(session, source_person_id)
    target_id = await current_person_id(session, target_person_id)
    if source_id == target_id:
        return target_id
    moment = now or datetime.now(UTC)
    members = list(
        await session.scalars(
            select(CanonicalPersonModel).where(
                or_(
                    CanonicalPersonModel.id == source_id,
                    CanonicalPersonModel.merged_into_person_id == source_id,
                )
            )
        )
    )
    source = next(person for person in members if person.id == source_id)
    target = await session.get(CanonicalPersonModel, target_id)
    assert target is not None
    configs = list(
        await session.scalars(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user",
                RuntimeConfigOverrideModel.canonical_person_id.in_((source_id, target_id)),
            )
        )
    )
    plugins = list(
        await session.scalars(
            select(PluginConfigValueModel).where(
                PluginConfigValueModel.scope_type == "user",
                PluginConfigValueModel.canonical_person_id.in_((source_id, target_id)),
            )
        )
    )
    source_route = await session.get(PersonActiveRouteModel, source_id)
    target_route = await session.get(PersonActiveRouteModel, target_id)
    source_alias = (
        await session.scalar(
            select(PersonAliasModel.id).where(
                PersonAliasModel.canonical_person_id == source_id,
                PersonAliasModel.canonical_space_id.is_(None),
                PersonAliasModel.alias == source.display_name,
            )
        )
        if source.display_name
        else None
    )
    # No history, external lookup, or broad catalogue scan follows the first DML.
    moved: list[dict[str, object]] = []
    config_keys = {row.config_key for row in configs if row.canonical_person_id == target_id}
    for row in configs:
        if row.canonical_person_id == source_id and row.config_key not in config_keys:
            moved.append(
                {
                    "table": row.__tablename__,
                    "id": row.id,
                    "version": row.version,
                    "from": source_id,
                    "to": target_id,
                }
            )
            row.canonical_person_id = target_id
    plugin_keys = {
        (row.plugin_id, row.key) for row in plugins if row.canonical_person_id == target_id
    }
    for plugin_row in plugins:
        if (
            plugin_row.canonical_person_id == source_id
            and (plugin_row.plugin_id, plugin_row.key) not in plugin_keys
        ):
            moved.append(
                {
                    "table": plugin_row.__tablename__,
                    "id": plugin_row.id,
                    "version": plugin_row.version,
                    "from": source_id,
                    "to": target_id,
                }
            )
            plugin_row.canonical_person_id = target_id
    if source.display_name and source_alias is None:
        session.add(
            PersonAliasModel(
                canonical_person_id=source_id,
                canonical_space_id=None,
                alias=source.display_name,
                alias_type="name",
                first_seen_at=moment,
                last_seen_at=moment,
            )
        )
    if not target.display_name:
        target.display_name = source.display_name
    target.is_superuser = target.is_superuser or source.is_superuser
    for member in members:
        member.merged_into_person_id = target_id
        member.revision += 1
        member.updated_at = moment
    target.revision += 1
    target.updated_at = moment
    await session.flush()
    if target_route is None and source_route is not None:
        session.add(
            PersonActiveRouteModel(
                person_id=target_id,
                identity_binding_id=source_route.identity_binding_id,
                presence_id=source_route.presence_id,
                route_generation=source_route.route_generation,
                paused=source_route.paused,
                revision=1,
                created_at=moment,
                updated_at=moment,
            )
        )
    session.add(
        AdminOperationEventModel(
            actor_user_id=actor_id,
            capability="identity.person.merge",
            operation="identity.person.merge",
            target_type="person",
            target_id=target_id,
            before_json=json.dumps({"source_person_id": source_id, "target_person_id": target_id}),
            after_json=json.dumps(
                {
                    "person_ids": [row.id for row in members],
                    "person_id": target_id,
                    "configuration_moves": moved,
                }
            ),
            success=True,
            duration_seconds=0,
            created_at=moment,
        )
    )
    return target_id
