"""C8 v1 dual-write of canonical identity shadows."""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.backfill_repository import sqlite_path_from_url
from qq_ai_bot.identity.backfill_service import IdentityBackfillService
from qq_ai_bot.identity.backfill_types import BackfillSettingsInput
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.dual_write import (
    IdentityDualWriteError,
    set_identity_failpoint,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError as DualWriteErrorAlias
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
    identity_write_settings,
    reset_identity_write_settings,
)
from qq_ai_bot.identity.writer_inventory import (
    C8_WRITER_INVENTORY,
    LEGACY_IDENTITY_MODELS,
    LEGACY_IDENTITY_TABLES,
    writer_keys,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, GroupModel, PersonModel
from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_SRC = Path("src/qq_ai_bot")
_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


def _same_instant(left: datetime, right: datetime) -> bool:
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    return _utc(left) == _utc(right)


_SQL_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"']?(\w+)[\"']?",
    re.IGNORECASE,
)


def _uow(database: Database) -> ScopedEventLedgerUnitOfWork:
    return ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())


def _settings(*, ignored: tuple[str, ...] = ("7777",)) -> None:
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"9000"}), ignored_bot_users=frozenset(ignored))
    )


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _arg_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _discover_legacy_writers() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in _SRC.rglob("*.py"):
        module = ".".join(path.with_suffix("").relative_to("src").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = _call_name(node)
                    if name in LEGACY_IDENTITY_MODELS:
                        found.add((module, fn.name, LEGACY_IDENTITY_MODELS[name]))
                    if name in {"insert", "update", "delete"} and node.args:
                        model = _arg_name(node.args[0])
                        if model in LEGACY_IDENTITY_MODELS:
                            found.add((module, fn.name, LEGACY_IDENTITY_MODELS[model]))
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for match in _SQL_WRITE.finditer(node.value):
                        table = match.group(1)
                        if table in LEGACY_IDENTITY_TABLES:
                            found.add((module, fn.name, table))
    return found


def test_ast_writer_inventory_covers_every_legacy_write() -> None:
    discovered = _discover_legacy_writers()
    inventory = writer_keys(C8_WRITER_INVENTORY)
    missing = sorted(discovered - inventory)
    assert missing == [], missing
    assert any(item.epoch == "c8" and "people" in item.tables for item in C8_WRITER_INVENTORY)
    assert any(item.epoch == "c7_offline" for item in C8_WRITER_INVENTORY)
    assert any(item.epoch == "c20" for item in C8_WRITER_INVENTORY)
    assert any(item.epoch == "c21" for item in C8_WRITER_INVENTORY)
    assert any(item.epoch == "c22" for item in C8_WRITER_INVENTORY)
    assert any(item.epoch == "c23" for item in C8_WRITER_INVENTORY)
    assert any(item.epoch == "c24" for item in C8_WRITER_INVENTORY)
    assert not any(item.epoch.startswith("defer_c2") for item in C8_WRITER_INVENTORY)


def test_write_settings_module_does_not_use_contextvar() -> None:
    tree = ast.parse(Path("src/qq_ai_bot/identity/write_settings.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)
    assert "contextvars" not in imported
    assert "ContextVar" not in imported


@pytest.mark.asyncio
async def test_identity_write_settings_are_process_wide_across_tasks() -> None:
    reset_identity_write_settings()
    started = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, frozenset[str]] = {}

    async def worker() -> None:
        started.set()
        await release.wait()
        seen["worker"] = identity_write_settings().ignored_bot_users

    task = asyncio.create_task(worker())
    await started.wait()
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset(), ignored_bot_users=frozenset({"7777"}))
    )
    seen["parent"] = identity_write_settings().ignored_bot_users
    release.set()
    await task
    assert seen["parent"] == frozenset({"7777"})
    assert seen["worker"] == frozenset({"7777"})
    reset_identity_write_settings()


@pytest.mark.asyncio
async def test_missing_identity_write_settings_fail_close(database: Database) -> None:
    reset_identity_write_settings()
    with pytest.raises(IdentityDualWriteError) as exc:
        await PeopleRepository(database).observe(user_id="1001", nickname="Ada")
    assert exc.value.category == "identity_write_settings"
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None


def test_dual_write_does_not_import_c7_cli_or_renderer() -> None:
    tree = ast.parse(Path("src/qq_ai_bot/identity/dual_write.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "qq_ai_bot.identity.backfill_repository" not in imported
    assert "qq_ai_bot.identity.backfill_service" not in imported
    assert "qq_ai_bot.cli" not in imported
    assert "qq_ai_bot.identity.reporting" not in imported
    assert DualWriteErrorAlias is IdentityDualWriteError


@pytest.mark.asyncio
async def test_enabled_flags_dual_write_person_and_space(database: Database) -> None:
    _settings()
    await PeopleRepository(database).set_enabled("1001", False)
    await GroupSettingsRepository(database).set_enabled("2001", True)
    async with database.sessions() as session:
        people = await session.get(PersonModel, "1001")
        group = await session.get(GroupModel, "2001")
        assert people is not None and people.canonical_person_id
        assert group is not None and group.canonical_space_id
        person = await session.get(CanonicalPersonModel, people.canonical_person_id)
        space = await session.get(CanonicalSpaceModel, group.canonical_space_id)
        assert person is not None and person.enabled is False
        assert space is not None and space.enabled is True


@pytest.mark.asyncio
async def test_human_private_and_group_event_shadows(database: Database) -> None:
    _settings()
    uow = _uow(database)
    private = await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="p1",
        sender_user_id="1001",
        sender_nickname="Ada",
        direction="inbound",
        content="hi",
    )
    group = await uow.append(
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id="g1",
        sender_user_id="1001",
        sender_nickname="Ada",
        direction="inbound",
        content="hall",
    )
    assert private.created and group.created
    async with database.sessions() as session:
        people = await session.get(PersonModel, "1001")
        bot = await session.get(PersonModel, "8000")
        group_row = await session.get(GroupModel, "2001")
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == "1001",
            )
        )
        presence = await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == IDENTITY_PLATFORM,
                PresenceModel.external_account_id == "8000",
            )
        )
        space = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == "2001",
            )
        )
        private_event = await session.get(ChatEventModel, private.event.id)
        group_event = await session.get(ChatEventModel, group.event.id)
        scope_row = await session.scalar(
            select(ConversationScopeModel).where(
                ConversationScopeModel.scope_key == ConversationScope.private("8000", "1001").key
            )
        )
        assert people is not None and people.canonical_person_id == binding.person_id
        assert bot is not None and bot.canonical_person_id is None and bot.is_bot
        assert group_row is not None and group_row.canonical_space_id == space.space_id
        assert presence is not None
        assert private_event is not None
        assert private_event.author_kind == "person"
        assert private_event.author_person_id == binding.person_id
        assert private_event.author_presence_id is None
        assert private_event.ingress_presence_id == presence.id
        assert private_event.canonical_event_id is None
        assert private_event.canonical_conversation_id is None
        assert private_event.ingress_provider is None
        assert group_event is not None
        assert group_event.author_kind == "person"
        assert group_event.canonical_event_id is None
        assert scope_row is not None
        assert scope_row.canonical_conversation_id is None
        person_count = (await session.scalars(select(CanonicalPersonModel))).all()
        assert len(person_count) == 1


@pytest.mark.asyncio
async def test_current_bot_overlapping_superuser_or_ignored_is_presence(
    database: Database,
) -> None:
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"9000"}), ignored_bot_users=frozenset({"7777"}))
    )
    uow = _uow(database)
    superuser_bot = await uow.append(
        scope=ConversationScope.private("9000", "1001"),
        platform_message_id="bot-super",
        sender_user_id="1001",
        direction="inbound",
        content="hi",
    )
    ignored_bot = await uow.append(
        scope=ConversationScope.group("7777", "2001"),
        platform_message_id="bot-ignored",
        sender_user_id="1001",
        direction="inbound",
        content="hall",
    )
    assert superuser_bot.created and ignored_bot.created
    async with database.sessions() as session:
        for external_id in ("9000", "7777"):
            people = await session.get(PersonModel, external_id)
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id == external_id
                )
            )
            presence = await session.scalar(
                select(PresenceModel).where(PresenceModel.external_account_id == external_id)
            )
            assert people is not None and people.is_bot and people.canonical_person_id is None
            assert binding is None
            assert presence is not None


@pytest.mark.asyncio
async def test_yuki_and_ignored_bot_do_not_create_person(database: Database) -> None:
    _settings()
    uow = _uow(database)
    yuki = await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="self-1",
        sender_user_id="8000",
        sender_is_bot=True,
        direction="outbound",
        content="reply",
    )
    ignored = await uow.append(
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id="bot-1",
        sender_user_id="7777",
        sender_is_bot=True,
        direction="inbound",
        content="ad",
    )
    async with database.sessions() as session:
        yuki_event = await session.get(ChatEventModel, yuki.event.id)
        ignored_event = await session.get(ChatEventModel, ignored.event.id)
        ignored_people = await session.get(PersonModel, "7777")
        bindings = (
            await session.scalars(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id.in_(("8000", "7777"))
                )
            )
        ).all()
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "8000")
        )
        assert yuki_event is not None
        assert yuki_event.author_kind == "yuki"
        assert yuki_event.author_presence_id == presence.id
        assert yuki_event.author_person_id is None
        assert ignored_event is not None
        assert ignored_event.author_kind == "external_bot"
        assert ignored_event.author_person_id is None
        assert ignored_event.author_presence_id is None
        assert ignored_people is not None
        assert ignored_people.canonical_person_id is None
        assert bindings == []


@pytest.mark.asyncio
async def test_canonical_preconfig_reused_without_fake_legacy_rows(database: Database) -> None:
    _settings()
    person_id = str(uuid4())
    binding_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person_id, enabled=True, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
        session.add(
            IdentityBindingModel(
                id=binding_id,
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="5555",
                display_name="pre",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    await _uow(database).append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="other",
        sender_user_id="1001",
        direction="inbound",
        content="hello",
    )
    async with database.sessions() as session:
        assert await session.get(PersonModel, "5555") is None
        leftover = await session.get(CanonicalPersonModel, person_id)
        assert leftover is not None
        assert leftover.revision == 1


@pytest.mark.asyncio
async def test_preconfig_binding_is_reused_on_first_legacy_observe(database: Database) -> None:
    _settings()
    person_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person_id, enabled=True, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1001",
                display_name="Ada",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    await PeopleRepository(database).observe(user_id="1001", nickname="Ada")
    async with database.sessions() as session:
        people = await session.get(PersonModel, "1001")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        person = await session.get(CanonicalPersonModel, person_id)
        assert people is not None
        assert people.canonical_person_id == person_id
        assert binding is not None and binding.person_id == person_id
        assert person is not None
        assert person.revision == 1
        assert _same_instant(person.updated_at, _NOW)
        assert len((await session.scalars(select(CanonicalPersonModel))).all()) == 1


@pytest.mark.asyncio
async def test_c7_backfill_then_new_write_reuses_identity(database: Database) -> None:
    _settings()
    async with database.sessions() as session, session.begin():
        session.add(
            PersonModel(
                user_id="1001",
                nickname="Ada",
                enabled=True,
                is_bot=False,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
            )
        )
    path = sqlite_path_from_url(database.url)
    report = IdentityBackfillService(
        path,
        BackfillSettingsInput(
            superusers=frozenset(),
            enabled_groups=frozenset(),
            ignored_bot_users=frozenset({"7777"}),
        ),
    ).apply()
    assert report.status == "succeeded"
    async with database.sessions() as session:
        before = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        assert before is not None
        person_id = before.person_id
        revision = (await session.get(CanonicalPersonModel, person_id)).revision
        updated = (await session.get(CanonicalPersonModel, person_id)).updated_at
    await _uow(database).append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="after-c7",
        sender_user_id="1001",
        direction="inbound",
        content="again",
    )
    async with database.sessions() as session:
        after = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        person = await session.get(CanonicalPersonModel, person_id)
        assert after is not None and after.person_id == person_id
        assert person is not None
        assert person.revision == revision
        assert person.updated_at == updated
        assert len((await session.scalars(select(CanonicalPersonModel))).all()) == 1


@pytest.mark.asyncio
async def test_retry_same_event_does_not_create_second_identity(database: Database) -> None:
    _settings()
    uow = _uow(database)
    first = await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="dup",
        sender_user_id="1001",
        direction="inbound",
        content="once",
    )
    second = await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="dup",
        sender_user_id="1001",
        direction="inbound",
        content="once",
    )
    assert first.created is True
    assert second.created is False
    async with database.sessions() as session:
        assert len((await session.scalars(select(CanonicalPersonModel))).all()) == 1
        assert len((await session.scalars(select(IdentityBindingModel))).all()) == 1
        assert len((await session.scalars(select(PresenceModel))).all()) == 1


@pytest.mark.asyncio
async def test_concurrent_first_seen_converges_to_one_identity(database: Database) -> None:
    _settings()
    people = PeopleRepository(database)

    async def worker(nickname: str) -> None:
        await people.observe(user_id="1001", nickname=nickname)

    await asyncio.gather(worker("Ada"), worker("Ada2"))
    async with database.sessions() as session:
        assert len((await session.scalars(select(CanonicalPersonModel))).all()) == 1
        assert len((await session.scalars(select(IdentityBindingModel))).all()) == 1
        row = await session.get(PersonModel, "1001")
        assert row is not None and row.canonical_person_id is not None


@pytest.mark.asyncio
async def test_failpoint_rolls_back_legacy_and_canonical(database: Database) -> None:
    _settings()

    def boom(name: str) -> None:
        if name == "after_identity_foundation":
            raise RuntimeError("failpoint")

    set_identity_failpoint(boom)
    try:
        with pytest.raises(RuntimeError, match="failpoint"):
            await PeopleRepository(database).observe(user_id="1001", nickname="Ada")
    finally:
        set_identity_failpoint(None)
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None
        assert (await session.scalars(select(CanonicalPersonModel))).all() == []
        assert (await session.scalars(select(IdentityBindingModel))).all() == []


@pytest.mark.asyncio
async def test_v2_and_malformed_state_fail_close(database: Database) -> None:
    _settings()
    await _flip_v2(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await PeopleRepository(database).observe(user_id="1001", nickname="Ada")
    assert exc.value.category == "unclassified"
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None
        await session.execute(text("DELETE FROM identity_runtime_state"))
        await session.commit()
    with pytest.raises(IdentityDualWriteError) as exc:
        await _uow(database).append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="v2-miss",
            sender_user_id="1001",
            direction="inbound",
            content="no",
        )
    assert exc.value.category == "identity_runtime_state"
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None
        assert (await session.scalars(select(ChatEventModel))).all() == []


@pytest.mark.asyncio
async def test_forgetme_clears_person_and_keeps_presence(database: Database) -> None:
    _settings()
    uow = _uow(database)
    await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="keep",
        sender_user_id="1001",
        direction="inbound",
        content="secret",
    )
    deleted = await PeopleRepository(database).delete_person("1001")
    assert deleted is True
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None
        assert (await session.scalars(select(IdentityBindingModel))).all() == []
        assert (await session.scalars(select(CanonicalPersonModel))).all() == []
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "8000")
        )
        bot = await session.get(PersonModel, "8000")
        assert presence is not None
        assert bot is not None
        leftover_events = (
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.author_person_id.is_not(None))
            )
        ).all()
        assert leftover_events == []
        assert (await session.scalars(select(CanonicalConversationModel))).all() == []


@pytest.mark.asyncio
async def test_forgetme_multi_binding_with_legacy_data_fail_closes(database: Database) -> None:
    _settings()
    person_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person_id, enabled=True, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
        for external_id in ("1001", "1002"):
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
            session.add(
                PersonModel(
                    user_id=external_id,
                    nickname="",
                    enabled=True,
                    is_bot=False,
                    first_seen_at=_NOW,
                    last_seen_at=_NOW,
                    canonical_person_id=person_id,
                )
            )
    with pytest.raises(IdentityDualWriteError) as exc:
        await PeopleRepository(database).delete_person("1001")
    assert exc.value.category == "forgetme_multiple_bindings"
    async with database.sessions() as session:
        assert await session.get(CanonicalPersonModel, person_id) is not None
        assert await session.get(PersonModel, "1001") is not None
        leftover = await session.get(PersonModel, "1002")
        assert leftover is not None
        assert leftover.canonical_person_id == person_id
        assert len((await session.scalars(select(IdentityBindingModel))).all()) == 2


@pytest.mark.asyncio
async def test_forgetme_canonical_only_extra_binding_is_removed(database: Database) -> None:
    _settings()
    person_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person_id, enabled=True, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
        session.add(
            PersonModel(
                user_id="1001",
                nickname="",
                enabled=True,
                is_bot=False,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                canonical_person_id=person_id,
            )
        )
        for external_id in ("1001", "1003"):
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
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        assert await session.get(CanonicalPersonModel, person_id) is None
        assert await session.get(PersonModel, "1001") is None
        assert await session.get(PersonModel, "1003") is None
        assert (await session.scalars(select(IdentityBindingModel))).all() == []


async def _running_plugin(database: Database, plugin_id: str = "c8-grant") -> None:
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id=plugin_id,
        name="C8",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="b" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish",),
    )
    await repository.approve(plugin_id)
    await repository.set_enabled(plugin_id, enabled=True)
    await repository.set_status(plugin_id, status="running")


@pytest.mark.asyncio
async def test_grant_target_syncs_presence_without_fake_people_row(database: Database) -> None:
    _settings()
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    await PluginNotificationRepository(database).grant_target(
        plugin_id="c8-grant",
        target=NotificationTarget(target_type="group", target_id="2001"),
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    async with database.sessions() as session:
        assert await session.get(PersonModel, "9999") is None
        assert (
            await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id == "9999"
                )
            )
            is None
        )
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "9999")
        )
        assert presence is not None


@pytest.mark.asyncio
async def test_author_kind_four_states_and_origin_stay_independent(database: Database) -> None:
    _settings()
    uow = _uow(database)
    person_event = await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="kind-person",
        sender_user_id="1001",
        direction="inbound",
        content="hi",
    )
    yuki_event = await uow.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="kind-yuki",
        sender_user_id="8000",
        sender_is_bot=True,
        direction="outbound",
        content="reply",
        origin="scheduled_automation",
    )
    bot_event = await uow.append(
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id="kind-bot",
        sender_user_id="7777",
        sender_is_bot=True,
        direction="inbound",
        content="ad",
    )
    external = await uow.append_external(
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id="kind-system",
        source_plugin_id="c8-grant",
        external_source="github",
        external_event_key="push-1",
        external_event_type="PushEvent",
        external_payload={"ok": True},
        external_target_id="2001",
        content="push",
        occurred_at=_NOW,
    )
    async with database.sessions() as session:
        person_row = await session.get(ChatEventModel, person_event.event.id)
        yuki_row = await session.get(ChatEventModel, yuki_event.event.id)
        bot_row = await session.get(ChatEventModel, bot_event.event.id)
        system_row = await session.get(ChatEventModel, external.event.id)
        assert person_row is not None and person_row.author_kind == "person"
        assert person_row.author_person_id is not None
        assert person_row.author_presence_id is None
        assert yuki_row is not None and yuki_row.author_kind == "yuki"
        assert yuki_row.origin == "scheduled_automation"
        assert yuki_row.author_person_id is None
        assert yuki_row.author_presence_id is not None
        assert bot_row is not None and bot_row.author_kind == "external_bot"
        assert bot_row.author_person_id is None
        assert bot_row.author_presence_id is None
        assert system_row is not None and system_row.author_kind == "system"
        assert system_row.origin == "plugin_background"
        assert system_row.event_kind == "external_event"
        assert system_row.author_person_id is None
        assert system_row.author_presence_id is None
        assert system_row.canonical_event_id is None
        assert system_row.canonical_conversation_id is None


@pytest.mark.asyncio
async def test_plugin_external_is_system_and_automation_outbound_is_yuki(
    database: Database,
) -> None:
    _settings()
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id="c8-grant",
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    receipt = await notifications.publish(
        plugin_id="c8-grant",
        request=PublishNotificationRequest(
            event_key="c8-external",
            event_type="PushEvent",
            external_source="github",
            target=target,
            occurred_at=_NOW,
            summary="push",
            payload={},
        ),
    )
    automation = await _uow(database).append(
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id="auto-out",
        sender_user_id="8000",
        sender_is_bot=True,
        direction="outbound",
        content="定时问候",
        origin="scheduled_automation",
    )
    async with database.sessions() as session:
        external_row = await session.get(ChatEventModel, receipt.source_event_id)
        auto_row = await session.get(ChatEventModel, automation.event.id)
        assert external_row is not None
        assert external_row.author_kind == "system"
        assert external_row.origin == "plugin_background"
        assert external_row.author_person_id is None
        assert external_row.author_presence_id is None
        assert auto_row is not None
        assert auto_row.author_kind == "yuki"
        assert auto_row.origin == "scheduled_automation"
        assert auto_row.author_presence_id is not None
