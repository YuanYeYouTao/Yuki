"""Current human ownership without rewriting original identity or request proofs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.engine import CursorResult
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


def current_person_enabled_condition(value: Any) -> ColumnElement[bool]:
    """Recipient qualification follows the current human, not an old actor source."""
    current = aliased(CanonicalPersonModel)
    return (
        select(current.id)
        .where(current.id == person_root_id(value), current.enabled.is_(True))
        .correlate_except(current)
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
    """Merge current material in the caller's consistent, atomic DB snapshot.

    Original Person, execution and assignment sources remain unchanged. Routing
    selects actual available candidates at the application boundary.
    """
    from yuki.admin.audit import redact_audit_value
    from yuki.persistence.models import (
        AdminOperationEventModel,
        PersonAliasModel,
        RuntimeConfigOverrideModel,
    )
    from yuki.plugin_host.db_models import PluginConfigValueModel

    roots = await current_person_ids(session, (source_person_id, target_person_id))
    if source_person_id not in roots or target_person_id not in roots:
        raise CanonicalIdentityError("missing_canonical_owner")
    source_id, target_id = roots[source_person_id], roots[target_person_id]
    if source_id == target_id:
        return target_id
    people = {
        person.id: person
        for person in await session.scalars(
            select(CanonicalPersonModel).where(CanonicalPersonModel.id.in_((source_id, target_id)))
        )
    }
    source, target = people[source_id], people[target_id]
    members = tuple(
        await session.scalars(
            select(CanonicalPersonModel.id)
            .where(
                or_(
                    CanonicalPersonModel.id == source_id,
                    CanonicalPersonModel.merged_into_person_id == source_id,
                )
            )
            .order_by(CanonicalPersonModel.id)
        )
    )
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
    scalar_keys = {row.config_key for row in configs if row.canonical_person_id == target_id}
    plugin_objects = {row.plugin_id for row in plugins if row.canonical_person_id == target_id}
    moved, discarded = [], []
    operations = []
    for model, rows in (
        (RuntimeConfigOverrideModel, configs),
        (PluginConfigValueModel, plugins),
    ):
        inherited, losing = [], []
        for row in rows:
            if row.canonical_person_id != source_id:
                continue
            record = {
                "table": model.__tablename__,
                "id": row.id,
                "version": row.version,
                "from": source_id,
                "to": target_id,
            }
            if isinstance(row, RuntimeConfigOverrideModel):
                conflict, key = row.config_key in scalar_keys, row.config_key
            else:
                conflict, key = row.plugin_id in plugin_objects, row.key
            if conflict:
                losing.append((row.id, row.version))
                snapshot = {
                    column.key: getattr(row, column.key) for column in model.__table__.columns
                }
                snapshot["value_json"] = json.dumps(
                    redact_audit_value(
                        json.loads(row.value_json),
                        key=key,
                    ),
                    ensure_ascii=False,
                )
                discarded.append(
                    {
                        **record,
                        "row": snapshot,
                    }
                )
            else:
                inherited.append((row.id, row.version))
                moved.append(record)
        operations.append((model, inherited, losing))
    moment = now or datetime.now(UTC)
    # Complete potentially large snapshots before the first DML/autoflush.
    before_json = json.dumps(
        {
            "source_person_id": source_id,
            "target_person_id": target_id,
            "discarded_configurations": discarded,
        },
        ensure_ascii=False,
        default=lambda value: value.isoformat(),
    )
    after_json = json.dumps(
        {"person_ids": members, "person_id": target_id, "configuration_moves": moved},
        ensure_ascii=False,
    )
    target_name = target.display_name or source.display_name
    target_superuser = target.is_superuser or source.is_superuser
    for model, inherited, losing in operations:
        if inherited:
            changed = await session.execute(
                update(model)
                .where(
                    tuple_(model.id, model.version).in_(inherited),
                    model.canonical_person_id == source_id,
                )
                .values(canonical_person_id=target_id)
            )
            if cast(CursorResult[Any], changed).rowcount != len(inherited):
                raise CanonicalIdentityError("canonical_owner_mismatch")
        if losing:
            removed = await session.execute(
                delete(model).where(
                    tuple_(model.id, model.version).in_(losing),
                    model.canonical_person_id == source_id,
                )
            )
            if cast(CursorResult[Any], removed).rowcount != len(losing):
                raise CanonicalIdentityError("canonical_owner_mismatch")
    await session.execute(
        update(CanonicalPersonModel)
        .where(
            or_(
                CanonicalPersonModel.id == source_id,
                CanonicalPersonModel.merged_into_person_id == source_id,
            )
        )
        .values(
            merged_into_person_id=target_id,
            revision=CanonicalPersonModel.revision + 1,
            updated_at=moment,
        )
    )
    await session.execute(
        update(CanonicalPersonModel)
        .where(CanonicalPersonModel.id == target_id)
        .values(
            display_name=target_name,
            is_superuser=target_superuser,
            revision=CanonicalPersonModel.revision + 1,
            updated_at=moment,
        )
    )
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
    session.add(
        AdminOperationEventModel(
            actor_user_id=actor_id,
            capability="identity.person.merge",
            operation="identity.person.merge",
            target_type="person",
            target_id=target_id,
            before_json=before_json,
            after_json=after_json,
            success=True,
            duration_seconds=0,
            created_at=moment,
        )
    )
    return target_id
