"""C24c control-query projections for Runtime Config and Emoji."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from qq_ai_bot.control_plane import (
    ControlPrincipal,
    ControlQueryError,
    ControlQueryService,
    Cursor,
    DecisionContext,
    ExternalIdVisibility,
    IdentityResolution,
    PageRequest,
    PrincipalSource,
    ProblemCode,
)
from qq_ai_bot.control_plane.query_types import ConfigOwnerKind
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId, SpaceId
from qq_ai_bot.emoji.db_models import EmojiAssetModel, EmojiScopeStateModel, EmojiUsageEventModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityRuntimeStateModel,
)
from qq_ai_bot.persistence.control_query import (
    ControlQueryAdapter,
    _config_owner_projection,
    _project_emoji_asset,
    _project_emoji_space_enablement,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel

_NOW = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
_CONFIG_KEY = "context.local_event_limit"
_SECRET_KEY = "llm.api_key"
_RAW_USER = "12345678901"
_RAW_GROUP = "20987654321"
_SECRET_VALUE = "sk-do-not-leak-c24c"


def _principal(*capabilities: str) -> ControlPrincipal:
    return ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=PersonId.new(),
        source=PrincipalSource.QQ,
        roles=("superuser",),
        granted_capabilities=capabilities,
        authenticated=True,
        active=True,
    )


def _context(
    principal: ControlPrincipal,
) -> DecisionContext[ControlPrincipal, PrincipalSource, object]:
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=principal.person_id or SpaceId.new(),
        reason="query",
    )


def _service(database: Database) -> ControlQueryService:
    return ControlQueryService(ControlQueryAdapter(database))


def _dump(page: object) -> str:
    items = getattr(page, "items", page)
    return json.dumps(
        [dataclasses.asdict(item) for item in items],
        default=str,
    )


async def _add_person(database: Database, person_id: str | None = None) -> str:
    token = person_id or str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=token, enabled=True, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
    return token


async def _add_space(database: Database, space_id: str | None = None) -> str:
    token = space_id or str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalSpaceModel(
                id=token,
                name="room",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return token


async def _set_complete_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = str(uuid4())
        row.source_fingerprint = "a" * 64
        row.completed_at = _NOW


async def _add_override(
    database: Database,
    *,
    key: str = _CONFIG_KEY,
    scope_type: str,
    scope_id: str,
    value: object,
    version: int = 1,
    canonical_person_id: str | None = None,
    canonical_space_id: str | None = None,
    apply_mode: str = "hot",
    value_type: str = "integer",
) -> int:
    async with database.sessions() as session, session.begin():
        row = RuntimeConfigOverrideModel(
            config_key=key,
            scope_type=scope_type,
            scope_id=scope_id,
            value_json=json.dumps(value),
            value_type=value_type,
            apply_mode=apply_mode,
            version=version,
            created_at=_NOW,
            updated_at=_NOW,
            updated_by=_RAW_USER,
            canonical_person_id=canonical_person_id,
            canonical_space_id=canonical_space_id,
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def _add_emoji(
    database: Database,
    *,
    asset_id: str | None = None,
    first_seen_user_id: str | None = _RAW_USER,
    first_seen_group_id: str | None = _RAW_GROUP,
    canonical_first_seen_person_id: str | None = None,
    canonical_first_seen_space_id: str | None = None,
) -> str:
    token = asset_id or str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            EmojiAssetModel(
                id=token,
                sha256=token.replace("-", "")[:64].ljust(64, "a"),
                relative_path=f"emoji/{token}.png",
                image_format="png",
                mime_type="image/png",
                byte_size=12,
                width=16,
                height=16,
                frame_count=1,
                animated=False,
                status="adopted",
                first_seen_user_id=first_seen_user_id,
                first_seen_group_id=first_seen_group_id,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                canonical_first_seen_person_id=canonical_first_seen_person_id,
                canonical_first_seen_space_id=canonical_first_seen_space_id,
            )
        )
    return token


async def _add_scope(
    database: Database,
    *,
    emoji_id: str,
    scope_type: str,
    scope_id: str,
    enabled: bool = True,
    canonical_space_id: str | None = None,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            EmojiScopeStateModel(
                emoji_id=emoji_id,
                scope_type=scope_type,
                scope_id=scope_id,
                enabled=enabled,
                weight=1.0,
                adopted_at=_NOW,
                updated_at=_NOW,
                canonical_space_id=canonical_space_id,
            )
        )


async def _add_usage(
    database: Database,
    *,
    emoji_id: str,
    actor_user_id: str,
    group_id: str,
    canonical_actor_person_id: str | None = None,
    canonical_space_id: str | None = None,
) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            EmojiUsageEventModel(
                emoji_id=emoji_id,
                actor_user_id=actor_user_id,
                group_id=group_id,
                trigger_message_id="m-c24c",
                source="reply",
                created_at=_NOW,
                canonical_actor_person_id=canonical_actor_person_id,
                canonical_space_id=canonical_space_id,
            )
        )


@pytest.mark.asyncio
async def test_v1_config_legacy_owner_is_masked_without_pii(database: Database) -> None:
    await _add_override(
        database,
        scope_type="user",
        scope_id=_RAW_USER,
        value=40,
    )
    await _add_override(
        database,
        scope_type="group",
        scope_id=_RAW_GROUP,
        value=50,
    )
    service = _service(database)
    page = await service.list_config_overrides(
        _context(_principal("control.config.read")),
        PageRequest(limit=20),
    )
    assert {item.owner_kind for item in page.items} == {
        ConfigOwnerKind.PERSON,
        ConfigOwnerKind.SPACE,
    }
    assert all(item.person_id is None and item.space_id is None for item in page.items)
    assert all(item.resolution is IdentityResolution.LEGACY for item in page.items)
    dumped = _dump(page)
    assert _RAW_USER not in dumped
    assert _RAW_GROUP not in dumped
    assert _RAW_USER not in dumped
    for item in page.items:
        assert item.legacy_owner is not None
        assert item.legacy_owner.visibility is ExternalIdVisibility.MASKED
        assert item.legacy_owner.value is None
        assert item.legacy_owner.configured is True


@pytest.mark.asyncio
async def test_v1_config_legacy_owner_reveals_only_with_existing_pii(
    database: Database,
) -> None:
    await _add_override(database, scope_type="user", scope_id=_RAW_USER, value=41)
    service = _service(database)
    revealed = await service.list_config_overrides(
        _context(_principal("control.config.read", "identity.binding.read_external")),
        PageRequest(limit=20),
    )
    assert revealed.items[0].legacy_owner is not None
    assert revealed.items[0].legacy_owner.visibility is ExternalIdVisibility.REVEALED
    assert revealed.items[0].legacy_owner.value == _RAW_USER
    assert revealed.items[0].person_id is None


@pytest.mark.asyncio
async def test_v2_config_projects_person_and_space_owners_not_raw_scope(
    database: Database,
) -> None:
    await _set_complete_v2(database)
    person = await _add_person(database)
    space = await _add_space(database)
    await _add_override(
        database,
        scope_type="global",
        scope_id="",
        value=30,
    )
    await _add_override(
        database,
        scope_type="user",
        scope_id=_RAW_USER,
        value=60,
        canonical_person_id=person,
    )
    await _add_override(
        database,
        scope_type="group",
        scope_id=_RAW_GROUP,
        value=70,
        canonical_space_id=space,
    )
    service = _service(database)
    page = await service.list_config_overrides(
        _context(_principal("control.config.read", "identity.binding.read_external")),
        PageRequest(limit=20),
    )
    by_kind = {item.owner_kind: item for item in page.items}
    assert by_kind[ConfigOwnerKind.GLOBAL].person_id is None
    assert by_kind[ConfigOwnerKind.PERSON].person_id == PersonId.parse(person)
    assert by_kind[ConfigOwnerKind.SPACE].space_id == SpaceId.parse(space)
    assert all(item.legacy_owner is None for item in page.items)
    dumped = _dump(page)
    assert _RAW_USER not in dumped
    assert _RAW_GROUP not in dumped
    assert person in dumped
    assert space in dumped
    effective = await service.list_effective_configs(
        _context(_principal("control.config.read")),
        PageRequest(limit=100),
    )
    written = [item for item in effective.items if item.key == _CONFIG_KEY]
    assert len(written) == 1
    assert written[0].owner_kind is ConfigOwnerKind.GLOBAL
    assert written[0].value == 30
    assert written[0].person_id is None
    assert written[0].space_id is None


@pytest.mark.asyncio
async def test_v2_corrupt_or_missing_canonical_is_unavailable_not_raw(
    database: Database,
) -> None:
    await _set_complete_v2(database)
    await _add_override(
        database,
        scope_type="user",
        scope_id=_RAW_USER,
        value=61,
    )
    service = _service(database)
    page = await service.list_config_overrides(
        _context(_principal("control.config.read", "identity.binding.read_external")),
        PageRequest(limit=20),
    )
    assert page.items
    assert all(item.owner_kind is ConfigOwnerKind.UNAVAILABLE for item in page.items)
    assert all(item.person_id is None and item.space_id is None for item in page.items)
    assert all(item.legacy_owner is None for item in page.items)
    dumped = _dump(page)
    assert _RAW_USER not in dumped


def test_corrupt_canonical_rows_project_unavailable_without_raw_ids() -> None:
    person = str(uuid4())
    space = str(uuid4())
    both = _config_owner_projection(
        SimpleNamespace(
            scope_type="user",
            scope_id=_RAW_USER,
            canonical_person_id=person,
            canonical_space_id=space,
        ),
        complete_v2=True,
        reveal_external=True,
    )
    malformed = _config_owner_projection(
        SimpleNamespace(
            scope_type="user",
            scope_id=_RAW_USER,
            canonical_person_id="not-a-canonical-id",
            canonical_space_id=None,
        ),
        complete_v2=True,
        reveal_external=True,
    )
    unknown_scope = _config_owner_projection(
        SimpleNamespace(
            scope_type="presence",
            scope_id=_RAW_USER,
            canonical_person_id=None,
            canonical_space_id=None,
        ),
        complete_v2=True,
        reveal_external=True,
    )
    emoji = _project_emoji_space_enablement(
        SimpleNamespace(scope_type="group", canonical_space_id="not-a-space", enabled=True),
        complete_v2=True,
    )
    assert both[0] is ConfigOwnerKind.UNAVAILABLE
    assert malformed[0] is ConfigOwnerKind.UNAVAILABLE
    assert unknown_scope[0] is ConfigOwnerKind.UNAVAILABLE
    assert all(
        item[1] is None and item[2] is None and item[4] is None
        for item in (both, malformed, unknown_scope)
    )
    assert emoji is not None
    assert emoji.space_id is None
    assert emoji.resolution is IdentityResolution.UNRESOLVED


@pytest.mark.asyncio
async def test_secret_override_is_configured_flag_only(database: Database) -> None:
    await _add_override(
        database,
        key=_SECRET_KEY,
        scope_type="global",
        scope_id="",
        value=_SECRET_VALUE,
        apply_mode="hot",
        value_type="string",
    )
    service = _service(database)
    context = _context(_principal("control.config.read"))
    effective = await service.list_effective_configs(context, PageRequest(limit=100))
    secrets = [item for item in effective.items if item.key == _SECRET_KEY]
    assert secrets
    assert secrets[0].value is None
    assert secrets[0].configured is True
    overrides = await service.list_config_overrides(context, PageRequest(limit=20))
    assert overrides.items[0].value is None
    assert overrides.items[0].configured is True
    assert overrides.items[0].apply_mode == "secret"
    dumped = _dump(effective) + _dump(overrides)
    assert _SECRET_VALUE not in dumped
    assert _RAW_USER not in dumped


@pytest.mark.asyncio
async def test_config_override_cursor_is_stable_keyset(database: Database) -> None:
    ids = [
        await _add_override(
            database,
            key=key,
            scope_type="global",
            scope_id="",
            value=index,
        )
        for index, key in enumerate(
            (_CONFIG_KEY, "memory.max_referenced_targets", "reply.delay_min_seconds"),
            start=1,
        )
    ]
    service = _service(database)
    context = _context(_principal("control.config.read"))
    seen: list[int] = []
    cursor = None
    while True:
        page = await service.list_config_overrides(context, PageRequest(limit=2, cursor=cursor))
        seen.extend(item.override_id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert "OFFSET" not in cursor.value
    assert seen == sorted(ids)
    with pytest.raises(ControlQueryError) as rejected:
        await service.list_config_overrides(
            context,
            PageRequest(limit=2, cursor=Cursor("c10v1|config|c|context.local_event_limit")),
        )
    assert rejected.value.problem.code is ProblemCode.VALIDATION_ERROR


async def _seed_v2_emoji_with_first_seen(database: Database) -> tuple[str, str, str]:
    await _set_complete_v2(database)
    person = await _add_person(database)
    space = await _add_space(database)
    asset_id = await _add_emoji(
        database,
        canonical_first_seen_person_id=person,
        canonical_first_seen_space_id=space,
    )
    await _add_scope(
        database,
        emoji_id=asset_id,
        scope_type="global",
        scope_id="",
        enabled=True,
    )
    await _add_scope(
        database,
        emoji_id=asset_id,
        scope_type="group",
        scope_id=_RAW_GROUP,
        enabled=True,
        canonical_space_id=space,
    )
    await _add_usage(
        database,
        emoji_id=asset_id,
        actor_user_id=_RAW_USER,
        group_id=_RAW_GROUP,
        canonical_actor_person_id=person,
        canonical_space_id=space,
    )
    return asset_id, person, space


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capabilities", "expect_person", "expect_space"),
    (
        (("control.emoji.read",), False, False),
        (("control.emoji.read", "identity.person.read"), True, False),
        (("control.emoji.read", "identity.space.read"), False, True),
        (("control.emoji.read", "identity.person.read", "identity.space.read"), True, True),
    ),
    ids=("neither", "person_only", "space_only", "both"),
)
async def test_emoji_first_seen_person_and_space_capabilities_are_independent(
    database: Database,
    capabilities: tuple[str, ...],
    expect_person: bool,
    expect_space: bool,
) -> None:
    asset_id, person, space = await _seed_v2_emoji_with_first_seen(database)
    page = await _service(database).list_emoji_assets(
        _context(_principal(*capabilities)),
        PageRequest(limit=20),
    )
    item = page.items[0]
    assert item.asset_id == asset_id
    assert item.first_seen_person_id == (PersonId.parse(person) if expect_person else None)
    assert item.first_seen_space_id == (SpaceId.parse(space) if expect_space else None)
    dumped = _dump(page)
    assert _RAW_USER not in dumped
    assert _RAW_GROUP not in dumped
    if expect_person:
        assert person in dumped
    else:
        assert person not in dumped
    if expect_space:
        assert space in dumped


def test_project_emoji_asset_first_seen_flags_are_independent() -> None:
    person = str(uuid4())
    space = str(uuid4())
    row = SimpleNamespace(
        id=str(uuid4()),
        status="adopted",
        pinned=False,
        canonical_first_seen_person_id=person,
        canonical_first_seen_space_id=space,
    )
    cases = (
        (False, False, None, None),
        (True, False, PersonId.parse(person), None),
        (False, True, None, SpaceId.parse(space)),
        (True, True, PersonId.parse(person), SpaceId.parse(space)),
    )
    for reveal_person, reveal_space, expected_person, expected_space in cases:
        view = _project_emoji_asset(
            row,
            scope_rows=[],
            complete_v2=True,
            reveal_first_seen_person=reveal_person,
            reveal_first_seen_space=reveal_space,
        )
        assert view.first_seen_person_id == expected_person
        assert view.first_seen_space_id == expected_space
        assert view.asset_id != person
        assert view.asset_id != space


@pytest.mark.asyncio
async def test_emoji_first_seen_is_hidden_and_not_owner(database: Database) -> None:
    asset_id, person, space = await _seed_v2_emoji_with_first_seen(database)
    service = _service(database)
    hidden = await service.list_emoji_assets(
        _context(_principal("control.emoji.read")),
        PageRequest(limit=20),
    )
    assert hidden.items[0].asset_id == asset_id
    assert hidden.items[0].first_seen_person_id is None
    assert hidden.items[0].first_seen_space_id is None
    assert hidden.items[0].global_enabled is True
    assert hidden.items[0].space_enablements[0].space_id == SpaceId.parse(space)
    assert hidden.items[0].space_enablements[0].resolution is IdentityResolution.CANONICAL
    dumped = _dump(hidden)
    assert _RAW_USER not in dumped
    assert _RAW_GROUP not in dumped
    assert person not in dumped
    assert space in dumped
    revealed = await service.list_emoji_assets(
        _context(_principal("control.emoji.read", "identity.person.read", "identity.space.read")),
        PageRequest(limit=20),
    )
    assert revealed.items[0].first_seen_person_id == PersonId.parse(person)
    assert revealed.items[0].first_seen_space_id == SpaceId.parse(space)
    assert revealed.items[0].asset_id != person
    assert revealed.items[0].asset_id != space
    revealed_dump = _dump(revealed)
    assert _RAW_USER not in revealed_dump
    assert _RAW_GROUP not in revealed_dump


@pytest.mark.asyncio
async def test_v1_emoji_space_enablement_omits_raw_group_id(database: Database) -> None:
    asset_id = await _add_emoji(database)
    await _add_scope(
        database,
        emoji_id=asset_id,
        scope_type="group",
        scope_id=_RAW_GROUP,
        enabled=False,
    )
    page = await _service(database).list_emoji_assets(
        _context(_principal("control.emoji.read", "identity.space.read")),
        PageRequest(limit=20),
    )
    assert page.items[0].space_enablements[0].space_id is None
    assert page.items[0].space_enablements[0].resolution is IdentityResolution.LEGACY
    assert page.items[0].space_enablements[0].enabled is False
    assert page.items[0].first_seen_person_id is None
    assert page.items[0].first_seen_space_id is None
    assert _RAW_GROUP not in _dump(page)
    assert _RAW_USER not in _dump(page)


@pytest.mark.asyncio
async def test_emoji_cursor_is_stable_and_usage_ids_never_leak(database: Database) -> None:
    ids = sorted([await _add_emoji(database, first_seen_user_id=f"u{index}") for index in range(3)])
    for asset_id in ids:
        await _add_usage(
            database,
            emoji_id=asset_id,
            actor_user_id="actor-qq-99999999",
            group_id=_RAW_GROUP,
        )
    service = _service(database)
    context = _context(_principal("control.emoji.read"))
    seen: list[str] = []
    cursor = None
    while True:
        page = await service.list_emoji_assets(context, PageRequest(limit=2, cursor=cursor))
        seen.extend(item.asset_id for item in page.items)
        dumped = _dump(page)
        assert "actor-qq-99999999" not in dumped
        assert _RAW_GROUP not in dumped
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == ids


@pytest.mark.asyncio
async def test_config_and_emoji_queries_deny_without_capability(database: Database) -> None:
    service = _service(database)
    with pytest.raises(ControlQueryError) as denied_config:
        await service.list_config_overrides(
            _context(_principal("control.emoji.read")),
            PageRequest(limit=5),
        )
    assert denied_config.value.problem.code is ProblemCode.CAPABILITY_DENIED
    with pytest.raises(ControlQueryError) as denied_emoji:
        await service.list_emoji_assets(
            _context(_principal("control.config.read")),
            PageRequest(limit=5),
        )
    assert denied_emoji.value.problem.code is ProblemCode.CAPABILITY_DENIED
