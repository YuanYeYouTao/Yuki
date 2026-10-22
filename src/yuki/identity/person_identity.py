"""Current human ownership without rewriting original identity or request proofs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, String, and_, case, delete, func, or_, select, tuple_, update
from sqlalchemy import cast as sql_cast
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


class PersonMergePreparationChanged(CanonicalIdentityError):
    def __init__(self) -> None:
        super().__init__("person_merge_preparation_changed")


@dataclass(frozen=True, slots=True)
class PersonMergePreparation:
    roots: tuple[tuple[str, str], ...]
    source_id: str
    target_id: str
    members: tuple[str, ...]
    people: tuple[tuple[str, str, bool], ...]
    source_alias: int | None
    configurations: tuple[tuple[Any, str, tuple[str, ...]], ...]
    operations: tuple[tuple[Any, list[tuple[int, int]], list[tuple[int, int]]], ...]
    before_json: str
    after_json: str
    source_name: str
    target_name: str
    target_superuser: bool


def _merge_config_selection(
    model: Any,
    source_id: str,
    target_id: str,
    discarded_ids: tuple[int, ...],
) -> Select[Any]:
    from yuki.persistence.models import RuntimeConfigOverrideModel

    keys = (
        (model.config_key,) if model is RuntimeConfigOverrideModel else (model.plugin_id, model.key)
    )
    relevant = (
        model.id,
        model.canonical_person_id,
        *keys,
        case((model.canonical_person_id == source_id, model.version)),
        *(case((model.id.in_(discarded_ids), column)) for column in model.__table__.columns),
    )
    return select(
        *(sql_cast(column, String).label(f"value_{index}") for index, column in enumerate(relevant))
    ).where(model.scope_type == "user", model.canonical_person_id.in_((source_id, target_id)))


async def prepare_person_merge(
    session: AsyncSession,
    source_person_id: str,
    target_person_id: str,
) -> PersonMergePreparation:
    from yuki.admin.audit import redact_audit_value
    from yuki.persistence.models import PersonAliasModel, RuntimeConfigOverrideModel
    from yuki.plugin_host.db_models import PluginConfigValueModel

    roots = await current_person_ids(session, (source_person_id, target_person_id))
    if source_person_id not in roots or target_person_id not in roots:
        raise CanonicalIdentityError("missing_canonical_owner")
    source_id, target_id = roots[source_person_id], roots[target_person_id]
    if source_id == target_id:
        return PersonMergePreparation(
            tuple(sorted(roots.items())),
            source_id,
            target_id,
            (),
            (),
            None,
            (),
            (),
            "{}",
            "{}",
            "",
            "",
            False,
        )
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
    # Complete potentially large configuration snapshots outside writer admission.
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
    configurations = []
    for model in (RuntimeConfigOverrideModel, PluginConfigValueModel):
        configuration_rows = sorted(
            tuple(row)
            for row in await session.execute(
                _merge_config_selection(
                    model,
                    source_id,
                    target_id,
                    tuple(
                        identity
                        for current, _, losing in operations
                        if current is model
                        for identity, _ in losing
                    ),
                )
            )
        )
        configurations.append(
            (model, json.dumps(configuration_rows), tuple(row[0] for row in configuration_rows))
        )
    return PersonMergePreparation(
        roots=tuple(sorted(roots.items())),
        source_id=source_id,
        target_id=target_id,
        members=members,
        people=tuple(
            sorted((row.id, row.display_name, row.is_superuser) for row in people.values())
        ),
        source_alias=source_alias,
        configurations=tuple(configurations),
        operations=tuple(operations),
        before_json=before_json,
        after_json=after_json,
        source_name=source.display_name,
        target_name=target_name,
        target_superuser=target_superuser,
    )


async def merge_persons(
    session: AsyncSession,
    source_person_id: str,
    target_person_id: str,
    *,
    prepared: PersonMergePreparation,
    now: datetime | None = None,
    actor_id: str = "system",
) -> str:
    """Recheck the original family/configuration selection, then atomically apply it."""
    from yuki.persistence.models import AdminOperationEventModel, PersonAliasModel

    roots = await current_person_ids(session, (source_person_id, target_person_id))
    if tuple(sorted(roots.items())) != prepared.roots:
        raise PersonMergePreparationChanged()
    source_id, target_id = prepared.source_id, prepared.target_id
    if source_id == target_id:
        return target_id
    members = await person_family_ids(session, source_id)
    if members != prepared.members:
        raise PersonMergePreparationChanged()
    people = tuple(
        sorted(
            tuple(row)
            for row in await session.execute(
                select(
                    CanonicalPersonModel.id,
                    CanonicalPersonModel.display_name,
                    CanonicalPersonModel.is_superuser,
                ).where(CanonicalPersonModel.id.in_((source_id, target_id)))
            )
        )
    )
    if people != prepared.people:
        raise PersonMergePreparationChanged()
    source_alias = (
        await session.scalar(
            select(PersonAliasModel.id).where(
                PersonAliasModel.canonical_person_id == source_id,
                PersonAliasModel.canonical_space_id.is_(None),
                PersonAliasModel.alias == prepared.source_name,
            )
        )
        if prepared.source_name
        else None
    )
    if source_alias != prepared.source_alias:
        raise PersonMergePreparationChanged()
    for model, original, expected_ids in prepared.configurations:
        discarded_ids = tuple(
            identity
            for current, _, losing in prepared.operations
            if current is model
            for identity, _ in losing
        )
        current = _merge_config_selection(model, source_id, target_id, discarded_ids).subquery()
        wanted = func.json_each(original).table_valued("value")
        columns = tuple(current.c)
        rows = tuple(
            await session.execute(
                select(
                    columns[0],
                    and_(
                        *(
                            column.is_(func.json_extract(wanted.c.value, f"$[{index}]"))
                            for index, column in enumerate(columns)
                        )
                    ),
                )
                .select_from(
                    current.outerjoin(
                        wanted, columns[0] == func.json_extract(wanted.c.value, "$[0]")
                    )
                )
                .order_by(columns[0])
            )
        )
        if tuple(row[0] for row in rows) != expected_ids or not all(row[1] for row in rows):
            raise PersonMergePreparationChanged()
    moment = now or datetime.now(UTC)
    operations = prepared.operations
    before_json, after_json = prepared.before_json, prepared.after_json
    target_name, target_superuser = prepared.target_name, prepared.target_superuser
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
    if prepared.source_name and source_alias is None:
        session.add(
            PersonAliasModel(
                canonical_person_id=source_id,
                canonical_space_id=None,
                alias=prepared.source_name,
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
