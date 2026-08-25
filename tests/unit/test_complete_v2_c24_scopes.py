"""C24a complete-v2 read gates for Runtime Config and Emoji."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.emoji.db_models import (
    EmojiAssetModel,
    EmojiScopeStateModel,
    EmojiUsageEventModel,
)
from qq_ai_bot.emoji.models import StoredEmojiMedia
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.identity.c24_scopes import (
    AMBIGUOUS_OWNER,
    CANONICAL_KIND_MISMATCH,
    CANONICAL_OWNER_DISABLED,
    CANONICAL_OWNER_MISMATCH,
    MISSING_CANONICAL_OWNER,
    try_live_person_id,
    try_live_space_id,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_presence_preconfig,
    ensure_canonical_space_preconfig,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    GroupModel,
    PersonModel,
    RuntimeConfigOverrideModel,
)

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-4466554400c4"


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _second_person_binding(database: Database, external_id: str, person_id: str) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_id,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _second_space_binding(database: Database, external_id: str, space_id: str) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=space_id,
                platform=IDENTITY_PLATFORM,
                external_space_id=external_id,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def _config(database: Database) -> RuntimeConfigService:
    return RuntimeConfigService(settings=make_settings(database.url), database=database)


def _media(storage: EmojiStorage, color: str = "red") -> StoredEmojiMedia:
    buffer = io.BytesIO()
    Image.new("RGB", (24, 20), color).save(buffer, format="PNG")
    content = buffer.getvalue()
    media = storage.inspect(content, near_duplicate_enabled=False)
    storage.persist(content, media)
    return media


async def _record_candidate(
    repository: EmojiRepository,
    media: StoredEmojiMedia,
    *,
    user_id: str | None,
    group_id: str | None,
):
    return await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=user_id,
        group_id=group_id,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )


async def _emoji_shadows(database: Database, sha256: str) -> tuple[str | None, str | None, int]:
    async with database.sessions() as session:
        row = await session.scalar(select(EmojiAssetModel).where(EmojiAssetModel.sha256 == sha256))
        assert row is not None
        return (
            row.canonical_first_seen_person_id,
            row.canonical_first_seen_space_id,
            row.seen_count,
        )


async def _emoji_asset(repository: EmojiRepository, storage: EmojiStorage) -> str:
    asset, _ = await _record_candidate(
        repository,
        _media(storage),
        user_id=None,
        group_id=None,
    )
    return asset.id


@pytest.mark.asyncio
async def test_v1_config_precedence_and_raw_scope_ids_stay_golden(database: Database) -> None:
    service = _config(database)
    await service.set_override(
        "context.local_event_limit",
        40,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="g",
    )
    await service.set_override(
        "context.local_event_limit",
        50,
        scope_type="group",
        scope_id="2001",
        actor_user_id="9000",
        trigger_message_id="grp",
    )
    await service.set_override(
        "context.local_event_limit",
        60,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="u",
    )
    assert (await service.get_effective("context.local_event_limit")).value == 40
    assert (await service.get_effective("context.local_event_limit", group_id="2001")).value == 50
    assert (
        await service.get_effective(
            "context.local_event_limit",
            user_id="1001",
            group_id="2001",
        )
    ).value == 60
    assert (
        await service.get_effective("context.local_event_limit", user_id="1002", group_id="2001")
    ).value == 50
    async with database.sessions() as session:
        user_row = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user",
                RuntimeConfigOverrideModel.scope_id == "1001",
            )
        )
        group_row = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "group",
                RuntimeConfigOverrideModel.scope_id == "2001",
            )
        )
    assert user_row is not None
    assert group_row is not None
    assert user_row.scope_id == "1001"
    assert group_row.scope_id == "2001"


@pytest.mark.asyncio
async def test_v2_two_bindings_share_user_and_group_config(database: Database) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    await _second_person_binding(database, "1002", person)
    await _second_space_binding(database, "2002", space)
    service = _config(database)
    written_user = await service.set_override(
        "context.local_event_limit",
        71,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="u",
    )
    written_group = await service.set_override(
        "context.local_event_limit",
        81,
        scope_type="group",
        scope_id="2001",
        actor_user_id="9000",
        trigger_message_id="g",
    )
    assert written_user.success
    assert written_user.scope_id == person
    assert written_group.success
    assert written_group.scope_id == space
    assert (await service.get_effective("context.local_event_limit", user_id="1002")).value == 71
    assert (await service.get_effective("context.local_event_limit", group_id="2002")).value == 81
    assert (
        await service.get_effective(
            "context.local_event_limit",
            user_id="1002",
            group_id="2002",
        )
    ).value == 71
    effective = await service.get_effective("context.local_event_limit", user_id="1002")
    assert effective.scope_id == person
    assert "1001" not in effective.scope_id
    assert "1002" not in effective.scope_id
    snapshot = await service.snapshot(user_id="1002", group_id="2002")
    assert snapshot.context.local_event_limit == 71
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
        user_rows = list(
            await session.scalars(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.scope_type == "user"
                )
            )
        )
        group_rows = list(
            await session.scalars(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.scope_type == "group"
                )
            )
        )
    assert people == 0
    assert groups == 0
    assert len(user_rows) == 1
    assert user_rows[0].canonical_person_id == person
    assert user_rows[0].canonical_space_id is None
    assert len(group_rows) == 1
    assert group_rows[0].canonical_space_id == space
    assert group_rows[0].canonical_person_id is None


@pytest.mark.asyncio
async def test_v2_presence_switch_keeps_config_and_emoji_lineage(
    database: Database,
    tmp_path: Path,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        await ensure_canonical_presence_preconfig(session, "8000", now=_NOW)
        await ensure_canonical_presence_preconfig(session, "8001", now=_NOW)
    service = _config(database)
    await service.set_override(
        "context.local_event_limit",
        64,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="u",
    )
    assert (await service.get_effective("context.local_event_limit", user_id="1001")).value == 64
    repository = EmojiRepository(database)
    emoji_id = await _emoji_asset(repository, EmojiStorage(tmp_path / "emoji"))
    await repository.adopt_scope(emoji_id, scope_type="group", scope_id="2001")
    selected = await repository.selectable(
        actor_user_id="1001",
        group_id="2001",
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=None,
        limit=10,
    )
    assert emoji_id in {asset.id for asset, _weight in selected}
    del person
    del space


@pytest.mark.asyncio
async def test_v2_missing_disabled_wrong_kind_and_ambiguous_fail_closed(
    database: Database,
) -> None:
    await _flip_v2(database)
    service = _config(database)
    with pytest.raises(IdentityDualWriteError) as missing:
        await service.get_effective("context.local_event_limit", user_id="1001")
    assert missing.value.category == MISSING_CANONICAL_OWNER
    assert "1001" not in str(missing.value)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        await ensure_canonical_presence_preconfig(session, "8000", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1003", now=_NOW)
    await service.set_override(
        "context.local_event_limit",
        55,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="u",
    )
    with pytest.raises(IdentityDualWriteError) as wrong_kind:
        await service.get_effective("context.local_event_limit", user_id="8000")
    assert wrong_kind.value.category == CANONICAL_KIND_MISMATCH
    assert "8000" not in str(wrong_kind.value)
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
        space_row = await session.get(CanonicalSpaceModel, space)
        assert space_row is not None
        space_row.enabled = False
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1003")
        )
        assert binding is not None
        binding.status = "disabled"
    with pytest.raises(IdentityDualWriteError) as disabled_person:
        await service.get_effective("context.local_event_limit", user_id="1001")
    assert disabled_person.value.category == CANONICAL_OWNER_DISABLED
    with pytest.raises(IdentityDualWriteError) as disabled_space:
        await service.get_effective("context.local_event_limit", group_id="2001")
    assert disabled_space.value.category == CANONICAL_OWNER_DISABLED
    with pytest.raises(IdentityDualWriteError) as disabled_binding:
        await service.get_effective("context.local_event_limit", user_id="1003")
    assert disabled_binding.value.category == CANONICAL_OWNER_DISABLED
    async with database.sessions() as session, session.begin():
        for owner, value in (("legacy-a", "61"), ("legacy-b", "62")):
            session.add(
                RuntimeConfigOverrideModel(
                    config_key="context.local_event_limit",
                    scope_type="user",
                    scope_id=owner,
                    value_json=value,
                    value_type="integer",
                    apply_mode="hot",
                    version=1,
                    created_at=_NOW,
                    updated_at=_NOW,
                    updated_by="9000",
                    canonical_person_id=person,
                )
            )
    enabled = await session_enable_person(database, person)
    assert enabled
    with pytest.raises(IdentityDualWriteError) as ambiguous:
        await service.get_effective("context.local_event_limit", user_id="1001")
    assert ambiguous.value.category in {CANONICAL_OWNER_MISMATCH, AMBIGUOUS_OWNER}
    assert "1001" not in str(ambiguous.value)
    assert "legacy-a" not in str(ambiguous.value)


async def session_enable_person(database: Database, person_id: str) -> bool:
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person_id)
        assert row is not None
        row.enabled = True
        return True


@pytest.mark.asyncio
async def test_v2_emoji_reads_canonical_space_and_actor(
    database: Database,
    tmp_path: Path,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    await _second_person_binding(database, "1002", person)
    await _second_space_binding(database, "2002", space)
    repository = EmojiRepository(database)
    emoji_id = await _emoji_asset(repository, EmojiStorage(tmp_path / "emoji"))
    await repository.adopt_scope(emoji_id, scope_type="group", scope_id="2001", weight=2.5)
    assert await repository.has_enabled_scope(emoji_id, scope_type="group", scope_id="2002")
    assert await repository.enabled_in_scope(emoji_id, group_id="2002")
    assert await repository.adopted_count(group_id="2002") == 1
    selected = {
        asset.id: weight
        for asset, weight in await repository.selectable(
            actor_user_id="1002",
            group_id="2002",
            cooldown_after=datetime.now(UTC),
            scope_cooldown_after=None,
            limit=10,
        )
    }
    assert selected[emoji_id] == 2.5
    replaceable = await repository.replaceable(scope_type="group", scope_id="2002")
    assert [asset.id for asset in replaceable] == [emoji_id]
    await repository.mark_used(
        emoji_id,
        actor_user_id="1001",
        group_id="2001",
        trigger_message_id="used",
        source="test",
    )
    cooled = await repository.selectable(
        actor_user_id="1002",
        group_id="2002",
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=datetime.now(UTC) - timedelta(seconds=60),
        limit=10,
    )
    assert cooled == ()
    await repository.mark_used(
        emoji_id,
        actor_user_id="1001",
        group_id=None,
        trigger_message_id="private",
        source="test",
    )
    private_cooled = await repository.selectable(
        actor_user_id="1002",
        group_id=None,
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=datetime.now(UTC) - timedelta(seconds=60),
        limit=10,
    )
    assert private_cooled == ()
    async with database.sessions() as session:
        scope_row = await session.scalar(select(EmojiScopeStateModel))
        usage_rows = list(await session.scalars(select(EmojiUsageEventModel)))
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
    assert scope_row is not None
    assert scope_row.canonical_space_id == space
    assert scope_row.scope_id == space
    assert {row.canonical_actor_person_id for row in usage_rows} == {person}
    assert {row.canonical_space_id for row in usage_rows} == {space, None}
    assert people == 0
    assert groups == 0


@pytest.mark.asyncio
async def test_v1_emoji_cooldown_stays_on_raw_group(database: Database, tmp_path: Path) -> None:
    repository = EmojiRepository(database)
    emoji_id = await _emoji_asset(repository, EmojiStorage(tmp_path / "emoji"))
    await repository.adopt_scope(emoji_id, scope_type="global")
    await repository.mark_used(
        emoji_id,
        actor_user_id="10001",
        group_id="group-a",
        trigger_message_id="used",
        source="test",
    )
    cooled_same = await repository.selectable(
        actor_user_id="10001",
        group_id="group-a",
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=datetime.now(UTC) - timedelta(seconds=60),
        limit=10,
    )
    other = await repository.selectable(
        actor_user_id="10001",
        group_id="group-b",
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=datetime.now(UTC) - timedelta(seconds=60),
        limit=10,
    )
    assert cooled_same == ()
    assert emoji_id in {asset.id for asset, _weight in other}


@pytest.mark.asyncio
async def test_v2_emoji_group_disabled_override_is_space_scoped(
    database: Database,
    tmp_path: Path,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        other = await ensure_canonical_space_preconfig(session, "2009", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    repository = EmojiRepository(database)
    emoji_id = await _emoji_asset(repository, EmojiStorage(tmp_path / "emoji"))
    await repository.adopt_scope(emoji_id, scope_type="global")
    await repository.set_group_enabled(emoji_id, group_id="2001", enabled=False)
    assert not await repository.enabled_in_scope(emoji_id, group_id="2001")
    assert await repository.enabled_in_scope(emoji_id, group_id="2009")
    selected_disabled = await repository.selectable(
        actor_user_id="1001",
        group_id="2001",
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=None,
        limit=10,
    )
    selected_other = await repository.selectable(
        actor_user_id="1001",
        group_id="2009",
        cooldown_after=datetime.now(UTC),
        scope_cooldown_after=None,
        limit=10,
    )
    assert emoji_id not in {asset.id for asset, _weight in selected_disabled}
    assert emoji_id in {asset.id for asset, _weight in selected_other}
    del other


@pytest.mark.asyncio
async def test_v2_emoji_first_seen_disabled_keeps_shadow_and_increments(
    database: Database,
    tmp_path: Path,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    repository = EmojiRepository(database)
    media = _media(EmojiStorage(tmp_path / "emoji"))
    first, created = await _record_candidate(repository, media, user_id="1001", group_id="2001")
    assert created
    assert first.seen_count == 1
    stamped_person, stamped_space, _ = await _emoji_shadows(database, media.sha256)
    assert stamped_person == person
    assert stamped_space == space
    async with database.sessions() as session, session.begin():
        person_row = await session.get(CanonicalPersonModel, person)
        space_row = await session.get(CanonicalSpaceModel, space)
        assert person_row is not None
        assert space_row is not None
        person_row.enabled = False
        space_row.enabled = False
    async with database.sessions() as session:
        assert await try_live_person_id(session, "1001") is None
        assert await try_live_space_id(session, "2001") is None
    second, created_again = await _record_candidate(
        repository, media, user_id="1001", group_id="2001"
    )
    assert created_again is False
    assert second.seen_count == 2
    kept_person, kept_space, _ = await _emoji_shadows(database, media.sha256)
    assert kept_person == person
    assert kept_space == space
    assert kept_person not in {"1001", "2001"}
    assert kept_space not in {"1001", "2001"}


@pytest.mark.asyncio
async def test_v2_emoji_first_seen_presence_wrong_kind_does_not_abort(
    database: Database,
    tmp_path: Path,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_canonical_presence_preconfig(session, "8000", now=_NOW)
    repository = EmojiRepository(database)
    media = _media(EmojiStorage(tmp_path / "emoji"), "blue")
    first, created = await _record_candidate(repository, media, user_id="8000", group_id=person)
    assert created
    assert first.seen_count == 1
    async with database.sessions() as session:
        assert await try_live_person_id(session, "8000") is None
        assert await try_live_space_id(session, person) is None
    first_person, first_space, _ = await _emoji_shadows(database, media.sha256)
    assert first_person is None
    assert first_space is None
    second, created_again = await _record_candidate(
        repository, media, user_id="8000", group_id=person
    )
    assert created_again is False
    assert second.seen_count == 2
    kept_person, kept_space, _ = await _emoji_shadows(database, media.sha256)
    assert kept_person is None
    assert kept_space is None
    assert str(IdentityDualWriteError(CANONICAL_KIND_MISMATCH)) == "identity dual-write failed"
    assert "8000" not in str(IdentityDualWriteError(CANONICAL_KIND_MISMATCH))


@pytest.mark.asyncio
async def test_v2_emoji_first_seen_ambiguous_or_rebound_keeps_original_shadow(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        other_person = await ensure_canonical_person_preconfig(session, "1999", now=_NOW)
    repository = EmojiRepository(database)
    media = _media(EmojiStorage(tmp_path / "emoji"), "green")
    _, created = await _record_candidate(repository, media, user_id="1001", group_id="2001")
    assert created
    stamped_person, _, _ = await _emoji_shadows(database, media.sha256)
    assert stamped_person == person
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        assert binding is not None
        binding.person_id = other_person
    rebound, created_rebound = await _record_candidate(
        repository, media, user_id="1001", group_id="2001"
    )
    assert created_rebound is False
    assert rebound.seen_count == 2
    rebound_person, rebound_space, _ = await _emoji_shadows(database, media.sha256)
    assert rebound_person == person
    assert rebound_person != other_person
    assert rebound_space == space

    async def _ambiguous_person(*_args: object, **_kwargs: object) -> str:
        raise IdentityDualWriteError(AMBIGUOUS_OWNER)

    async def _ambiguous_space(*_args: object, **_kwargs: object) -> str:
        raise IdentityDualWriteError(AMBIGUOUS_OWNER)

    monkeypatch.setattr(
        "qq_ai_bot.identity.c24_scopes.resolve_live_person_id",
        _ambiguous_person,
    )
    monkeypatch.setattr(
        "qq_ai_bot.identity.c24_scopes.resolve_live_space_id",
        _ambiguous_space,
    )
    async with database.sessions() as session:
        assert await try_live_person_id(session, "1001") is None
        assert await try_live_space_id(session, "2001") is None
    third, created_third = await _record_candidate(
        repository, media, user_id="1001", group_id="2001"
    )
    assert created_third is False
    assert third.seen_count == 3
    kept_person, kept_space, _ = await _emoji_shadows(database, media.sha256)
    assert kept_person == person
    assert kept_space == space
    assert kept_person not in {"1001", "2001", "1999"}
    assert "1001" not in str(IdentityDualWriteError(AMBIGUOUS_OWNER))


@pytest.mark.asyncio
async def test_v2_emoji_first_seen_disabled_insert_does_not_invent_owner(
    database: Database,
    tmp_path: Path,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        person_row = await session.get(CanonicalPersonModel, person)
        space_row = await session.get(CanonicalSpaceModel, space)
        assert person_row is not None
        assert space_row is not None
        person_row.enabled = False
        space_row.enabled = False
    repository = EmojiRepository(database)
    media = _media(EmojiStorage(tmp_path / "emoji"), "yellow")
    asset, created = await _record_candidate(repository, media, user_id="1001", group_id="2001")
    assert created
    assert asset.seen_count == 1
    shadow_person, shadow_space, _ = await _emoji_shadows(database, media.sha256)
    assert shadow_person is None
    assert shadow_space is None
    assert "1001" not in str(IdentityDualWriteError(CANONICAL_OWNER_DISABLED))


@pytest.mark.asyncio
async def test_v2_config_result_and_audit_hide_legacy_unique_scope_key(
    database: Database,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    await _second_person_binding(database, "1002", person)
    await _second_space_binding(database, "2002", space)
    async with database.sessions() as session, session.begin():
        session.add(
            RuntimeConfigOverrideModel(
                config_key="context.local_event_limit",
                scope_type="user",
                scope_id="1001",
                value_json="61",
                value_type="integer",
                apply_mode="hot",
                version=1,
                created_at=_NOW,
                updated_at=_NOW,
                updated_by="9000",
                canonical_person_id=person,
            )
        )
        session.add(
            RuntimeConfigOverrideModel(
                config_key="context.local_event_limit",
                scope_type="group",
                scope_id="2001",
                value_json="71",
                value_type="integer",
                apply_mode="hot",
                version=1,
                created_at=_NOW,
                updated_at=_NOW,
                updated_by="9000",
                canonical_space_id=space,
            )
        )
    service = _config(database)
    written_user = await service.set_override(
        "context.local_event_limit",
        77,
        scope_type="user",
        scope_id="1002",
        actor_user_id="9000",
        trigger_message_id="legacy-user",
    )
    written_group = await service.set_override(
        "context.local_event_limit",
        88,
        scope_type="group",
        scope_id="2002",
        actor_user_id="9000",
        trigger_message_id="legacy-group",
    )
    assert written_user.success
    assert written_user.scope_id == person
    assert written_group.success
    assert written_group.scope_id == space
    effective_user = await service.get_effective("context.local_event_limit", user_id="1002")
    effective_group = await service.get_effective("context.local_event_limit", group_id="2002")
    assert effective_user.scope_id == person
    assert effective_group.scope_id == space
    async with database.sessions() as session:
        stored_user = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user"
            )
        )
        stored_group = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "group"
            )
        )
    assert stored_user is not None
    assert stored_user.scope_id == "1001"
    assert stored_user.canonical_person_id == person
    assert stored_user.version == 2
    assert stored_group is not None
    assert stored_group.scope_id == "2001"
    assert stored_group.canonical_space_id == space
    assert stored_group.version == 2
    history = await service.history(key="context.local_event_limit")
    raw_scope_ids = {"1001", "1002", "2001", "2002"}
    assert written_user.scope_id not in raw_scope_ids
    assert written_group.scope_id not in raw_scope_ids
    assert effective_user.scope_id not in raw_scope_ids
    assert effective_group.scope_id not in raw_scope_ids
    for event in history:
        for state in (event.before, event.after):
            if isinstance(state, dict) and "scope_id" in state:
                assert state["scope_id"] in {person, space, ""}
                assert state["scope_id"] not in raw_scope_ids
    deleted = await service.delete_override(
        "context.local_event_limit",
        scope_type="user",
        scope_id="1002",
        actor_user_id="9000",
        trigger_message_id="legacy-del",
    )
    assert deleted.success
    assert deleted.scope_id == person
    async with database.sessions() as session:
        user_row = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user"
            )
        )
        group_row = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "group"
            )
        )
    assert user_row is None
    assert group_row is not None
    assert group_row.scope_id == "2001"
    assert group_row.canonical_space_id == space
    assert group_row.version == 2
