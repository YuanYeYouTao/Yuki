"""C22: automation canonical targets at send time."""

from __future__ import annotations

import ast
import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, or_, select, text
from tests.conftest import make_settings
from tests.support.gateway import napcat_registry
from tests.unit.test_control_query_projections import _context, _principal
from tests.unit.test_control_query_projections import _service as _query
from tests.unit.test_identity_backfill import (
    _create_schema,
    _insert_group,
    _insert_people,
    _open,
)
from tests.unit.test_identity_backfill import (
    _service as _backfill_service,
)
from tests.unit.test_identity_backfill import (
    _settings as _backfill_settings,
)
from tests.unit.test_identity_cutover import (
    _insert_automation_row,
    _prepare_snapshots,
    _seed_identity,
)
from tests.unit.test_identity_cutover import (
    _open as _cutover_open,
)
from tests.unit.test_identity_cutover import (
    _service as _cutover_service,
)
from tests.unit.test_migration_0043 import _upgrade

from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import OneBotProactiveGateway
from qq_ai_bot.automation.models import AutomationScript, RunStatus
from qq_ai_bot.automation.registry import CapabilityResult, build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.control_plane.paging import PageRequest
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
)
from qq_ai_bot.conversation.hydrate import conversation_for_owner, primary_alias_for_conversation
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.backfill_repository import IdentityBackfillRepository
from qq_ai_bot.identity.c22_automation import (
    C22_AUTOMATION_INCOMPLETE,
    require_c22_runnable_automation_targets,
)
from qq_ai_bot.identity.db_models import (
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    PresenceModel,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_presence_preconfig,
    ensure_canonical_space_preconfig,
)
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import EVENT_AUTHOR_KINDS, IDENTITY_PLATFORM
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AutomationModel, ChatEventModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository
from qq_ai_bot.time.service import TimeContextService

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_NOW_TEXT = "2026-08-24T00:00:00+00:00"
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"
_SRC = Path("src/qq_ai_bot")
_AUTOMATION_FILES = (
    _SRC / "automation" / "context.py",
    _SRC / "automation" / "executor.py",
    _SRC / "automation" / "gateway.py",
    _SRC / "automation" / "handlers.py",
    _SRC / "automation" / "repository.py",
    _SRC / "automation" / "worker.py",
    _SRC / "identity" / "c22_automation.py",
    _SRC / "application" / "modules" / "automation.py",
)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class _Bot:
    self_id: str
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def call_api(self, action: str, **params: object) -> dict[str, object]:
        self.calls.append((action, dict(params)))
        return {"message_id": len(self.calls)}


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


def _script(*, user_id: str = "$creator_user_id", group_id: str | None = None) -> AutomationScript:
    if group_id is not None:
        step = {
            "id": "send",
            "call": "onebot.send_group_message",
            "arguments": {"group_id": group_id, "text": "测"},
        }
    else:
        step = {
            "id": "send",
            "call": "onebot.send_private_message",
            "arguments": {"user_id": user_id, "text": "测"},
        }
    return AutomationScript.model_validate(
        {
            "version": 1,
            "name": "提醒",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "none", "include_memories": False, "history_limit": 8},
            "steps": [step],
            "limits": {
                "max_steps": 1,
                "max_llm_calls": 0,
                "max_tool_calls": 1,
                "max_messages": 1,
                "timeout_seconds": 30,
            },
        }
    )


def _inbound(
    user_id: str,
    *targets: str,
    bot_user_id: str = "8000",
    group_id: str | None = None,
) -> InboundMessage:
    text = "1秒后提醒 " + " ".join(targets)
    return InboundMessage(
        message_id=f"auto-{uuid4()}",
        event_type="group" if group_id else "private",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="用户"),
        text=text,
        raw_text=text,
        bot_user_id=bot_user_id,
        group_id=group_id,
        mentioned_user_ids=targets,
    )


def _generate_script() -> AutomationScript:
    return AutomationScript.model_validate(
        {
            "version": 1,
            "name": "读历史",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "none", "history_limit": 8},
            "steps": [
                {
                    "id": "gen",
                    "call": "yuki.generate",
                    "arguments": {"instruction": "看", "context_profile": "none"},
                }
            ],
            "limits": {
                "max_steps": 1,
                "max_llm_calls": 1,
                "max_tool_calls": 1,
                "max_messages": 1,
                "timeout_seconds": 30,
            },
        }
    )


def _send_registry():
    async def send_private(arguments, context):
        await context.gateway.send_private(str(arguments["user_id"]), str(arguments["text"]))
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    async def send_group(arguments, context):
        await context.gateway.send_group(str(arguments["group_id"]), str(arguments["text"]))
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    return build_capability_registry(
        {
            "onebot.send_private_message": send_private,
            "onebot.send_group_message": send_group,
        }
    )


def _service(database: Database, *, superusers: str = "9000") -> AutomationService:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv=superusers)
    return AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database, clock=clock),
    )


def _insert_legacy_automation(
    connection: sqlite3.Connection,
    *,
    creator: str,
    script: dict[str, object],
    authority: dict[str, object],
    status: str = "active",
    target_person: str | None = None,
    target_space: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO automations("
        "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
        "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
        "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
        "created_at, updated_at, canonical_target_person_id, canonical_target_space_id"
        ") VALUES (?, '8000', 'legacy', ?, 'Asia/Shanghai', '{}', ?, 'hash', '[]', ?, "
        "?, 0, 0, 1800, ?, ?, ?, ?)",
        (
            creator,
            status,
            json.dumps(script, ensure_ascii=False),
            json.dumps(authority, ensure_ascii=False),
            f"event-{uuid4()}",
            _NOW_TEXT,
            _NOW_TEXT,
            target_person,
            target_space,
        ),
    )
    return int(cursor.lastrowid)


async def _new_run(repository: AutomationRepository, automation_id: int, clock: FakeClock):
    clock.advance(2)
    run = await repository.create_run(
        automation_id, scheduled_for=clock.now(), actual_started_at=clock.now()
    )
    assert run is not None
    return run


async def _add_conversation(
    session,
    *,
    conversation_id: str,
    kind: str,
    owner_id: str,
    scope_key: str,
    generation: int = 1,
) -> None:
    alias_id = str(uuid4())
    session.add(
        CanonicalConversationModel(
            id=conversation_id,
            kind=kind,
            person_id=owner_id if kind == "private" else None,
            space_id=owner_id if kind == "space" else None,
            primary_alias_id=alias_id,
            primary_marker=1,
            generation=generation,
            starts_after_event_id=0,
            last_event_id=0,
            last_generation_change_event_id=0,
            covered_through_event_id=0,
            uncovered_event_count=0,
            uncovered_character_count=0,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.add(
        ConversationLegacyAliasModel(
            id=alias_id,
            conversation_id=conversation_id,
            scope_key=scope_key,
            is_primary=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def test_backfill_fills_person_and_space_targets_without_conversation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "c22-backfill.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _insert_group(connection, "2001")
        person_auto = _insert_legacy_automation(
            connection,
            creator="1001",
            script={
                "steps": [
                    {
                        "call": "onebot.send_private_message",
                        "arguments": {"user_id": "1001"},
                    }
                ]
            },
            authority={"creator_user_id": "1001"},
        )
        space_auto = _insert_legacy_automation(
            connection,
            creator="1001",
            script={
                "steps": [
                    {
                        "call": "onebot.send_group_message",
                        "arguments": {"group_id": "2001"},
                    }
                ]
            },
            authority={"creator_user_id": "1001", "current_group_id": "2001"},
            status="paused",
        )
        cancelled = _insert_legacy_automation(
            connection,
            creator="1001",
            script={"steps": []},
            authority={"creator_user_id": "1001"},
            status="cancelled",
        )
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0
        before = IdentityBackfillRepository(path).business_signature(connection)
    first = _backfill_service(
        path, _backfill_settings(superusers=("1001",), enabled_groups=("2001",))
    ).apply()
    assert first.status == "succeeded", first.conflicts
    assert first.business_diff == 1
    assert first.counts.automation_targets == 2
    with _open(path) as connection:
        person_row = connection.execute(
            "SELECT canonical_target_person_id, canonical_target_space_id "
            "FROM automations WHERE id = ?",
            (person_auto,),
        ).fetchone()
        space_row = connection.execute(
            "SELECT canonical_target_person_id, canonical_target_space_id "
            "FROM automations WHERE id = ?",
            (space_auto,),
        ).fetchone()
        cancelled_row = connection.execute(
            "SELECT canonical_target_person_id, canonical_target_space_id "
            "FROM automations WHERE id = ?",
            (cancelled,),
        ).fetchone()
        assert person_row[0] and person_row[1] is None
        assert space_row[0] is None and space_row[1]
        assert cancelled_row[0] is None and cancelled_row[1] is None
        creator = connection.execute(
            "SELECT canonical_creator_person_id FROM automations WHERE id = ?",
            (person_auto,),
        ).fetchone()[0]
        owner = connection.execute(
            "SELECT person_id FROM identity_bindings WHERE external_account_id = '1001'"
        ).fetchone()[0]
        assert creator == owner
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        after = IdentityBackfillRepository(path).business_signature(connection)
        assert after != before
    second = _backfill_service(
        path, _backfill_settings(superusers=("1001",), enabled_groups=("2001",))
    ).apply()
    assert second.status == "succeeded"
    assert second.business_diff == 0
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).business_signature(connection) == after
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


def test_backfill_conflict_is_content_free_and_zero_write(tmp_path: Path) -> None:
    path = tmp_path / "c22-conflict.db"
    _create_schema(path)
    other = str(uuid4())
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (other, _NOW_TEXT, _NOW_TEXT),
        )
        _insert_legacy_automation(
            connection,
            creator="1001",
            script={
                "steps": [
                    {
                        "call": "onebot.send_private_message",
                        "arguments": {"user_id": "1001"},
                    }
                ]
            },
            authority={"creator_user_id": "1001"},
            target_person=other,
        )
        connection.commit()
        before = IdentityBackfillRepository(path).natural_key_projection(connection)
        before_targets = list(
            connection.execute(
                "SELECT id, canonical_target_person_id, canonical_target_space_id FROM automations"
            )
        )
    report = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert report.status == "conflicted"
    assert report.business_diff == 0
    assert report.conflicts
    for item in report.conflicts:
        assert item.error_category in {"ambiguous_owner", "missing_owner"}
        assert "1001" not in item.fingerprint
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).natural_key_projection(connection) == before
        assert (
            list(
                connection.execute(
                    "SELECT id, canonical_target_person_id, canonical_target_space_id "
                    "FROM automations"
                )
            )
            == before_targets
        )
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


def test_cutover_blocks_null_wrong_owner_and_xor_then_accepts_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "c22-cutover.db"
    _upgrade(live, monkeypatch, "head")
    with _cutover_open(live) as connection:
        ids = _seed_identity(connection)
        _insert_automation_row(
            connection,
            canonical_creator_person_id=ids["person"],
            canonical_presence_id=ids["presence"],
        )
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as incomplete:
            require_c22_runnable_automation_targets(connection)
        assert incomplete.value.category == C22_AUTOMATION_INCOMPLETE
    settings = _prepare_snapshots(live, tmp_path / "c22-null-snap")
    assert _cutover_service(live, settings).plan().error_category == C22_AUTOMATION_INCOMPLETE

    with _cutover_open(live) as connection:
        connection.execute("DELETE FROM automations")
        connection.commit()
        missing = str(uuid4())
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO automations("
            "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
            "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
            "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
            "created_at, updated_at, canonical_creator_person_id, canonical_presence_id, "
            "canonical_target_person_id"
            ") VALUES ('1001', '8000', 'auto', 'active', 'Asia/Shanghai', '{}', '{}', "
            "'hash', '[]', '{}', 'event-wrong', 0, 0, 1800, ?, ?, ?, ?, ?)",
            (_NOW_TEXT, _NOW_TEXT, ids["person"], ids["presence"], missing),
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as wrong:
            require_c22_runnable_automation_targets(connection)
        assert wrong.value.category == C22_AUTOMATION_INCOMPLETE

    with _cutover_open(live) as connection:
        connection.execute("DELETE FROM automations")
        connection.execute("DROP TRIGGER IF EXISTS trg_automations_extension_shadow_insert")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO automations("
            "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
            "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
            "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
            "created_at, updated_at, canonical_creator_person_id, canonical_presence_id, "
            "canonical_target_person_id, canonical_target_space_id"
            ") VALUES ('1001', '8000', 'auto', 'active', 'Asia/Shanghai', '{}', '{}', "
            "'hash', '[]', '{}', 'event-xor', 0, 0, 1800, ?, ?, ?, ?, ?, ?)",
            (
                _NOW_TEXT,
                _NOW_TEXT,
                ids["person"],
                ids["presence"],
                ids["person"],
                ids["space"],
            ),
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as both:
            require_c22_runnable_automation_targets(connection)
        assert both.value.category == C22_AUTOMATION_INCOMPLETE

    with _cutover_open(live) as connection:
        connection.execute("DELETE FROM automations")
        connection.execute(
            "INSERT INTO automations("
            "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
            "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
            "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
            "created_at, updated_at, canonical_creator_person_id, canonical_presence_id, "
            "canonical_target_person_id"
            ") VALUES ('1001', '8000', 'auto', 'active', 'Asia/Shanghai', '{}', '{}', "
            "'hash', '[]', '{}', 'event-ok', 0, 0, 1800, ?, ?, ?, ?, ?)",
            (_NOW_TEXT, _NOW_TEXT, ids["person"], ids["presence"], ids["person"]),
        )
        connection.commit()
        require_c22_runnable_automation_targets(connection)
    settings = _prepare_snapshots(live, tmp_path / "c22-ok-snap")
    report = _cutover_service(live, settings).plan()
    assert report.status == "succeeded", report.error_category

    with _cutover_open(live) as connection:
        connection.execute("DELETE FROM automations")
        connection.execute(
            "INSERT INTO automations("
            "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
            "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
            "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
            "created_at, updated_at, canonical_presence_id, canonical_target_person_id"
            ") VALUES ('1001', '8000', 'auto', 'active', 'Asia/Shanghai', '{}', '{}', "
            "'hash', '[]', '{}', 'event-no-creator', 0, 0, 1800, ?, ?, ?, ?)",
            (_NOW_TEXT, _NOW_TEXT, ids["presence"], ids["person"]),
        )
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as no_creator:
            require_c22_runnable_automation_targets(connection)
        assert no_creator.value.category == C22_AUTOMATION_INCOMPLETE


@pytest.mark.asyncio
async def test_person_send_follows_route_without_rewriting_task(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c22-person")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_canonical_presence_preconfig(session, "8000")
        presence_b = await ensure_canonical_presence_preconfig(session, "8001")
        creator = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "10001", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=str(uuid4()),
            kind="private",
            owner_id=person,
            scope_key="bot:8000:private:10001",
        )
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_person(person) == "taken"
    row = await _service(database).create(
        _script(user_id="10001"),
        inbound=_inbound("9000", "10001"),
        conversation_key="private:9000",
    )
    assert row.canonical_target_person_id == person
    assert row.bot_user_id == "8000"
    async with database.sessions() as session:
        conversation = await conversation_for_owner(session, kind="private", person_id=person)
        assert conversation is not None
        generation = conversation.generation
        primary = await primary_alias_for_conversation(session, conversation.id)
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)
    ledger = EventLedgerRepository(database)
    executor = AutomationExecutor(
        settings=settings,
        registry=_send_registry(),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=lambda context: OneBotProactiveGateway(
            bot_user_id=context.bot_user_id,
            creator_user_id=context.creator_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            ledger=ledger,
            actions=AgentActionRepository(database),
            registry=registry,
            router=router,
            target_person_id=context.canonical_target_person_id,
            target_space_id=context.canonical_target_space_id,
        ),
        router=router,
    )
    first_run = await _new_run(repository, row.id, clock)
    first = await executor.execute(row, first_run)
    assert first.status is RunStatus.SUCCEEDED
    assert first.messages_sent == 1
    assert bot_a.calls and bot_a.calls[0][0] == "send_private_msg"
    assert bot_b.calls == []
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    assert await router.cas_takeover_person(person) == "taken"
    same = await repository.get(row.id)
    assert same is not None
    second_run = await _new_run(repository, same.id, clock)
    second = await executor.execute(same, second_run)
    assert second.status is RunStatus.SUCCEEDED
    assert bot_b.calls and bot_b.calls[0][0] == "send_private_msg"
    assert len(bot_a.calls) == 1
    async with database.sessions() as session:
        refreshed = await repository.get(row.id)
        conversation = await conversation_for_owner(session, kind="private", person_id=person)
        assert refreshed is not None
        assert refreshed.canonical_target_person_id == person
        assert refreshed.bot_user_id == "8000"
        assert conversation is not None
        assert conversation.generation == generation
        assert await primary_alias_for_conversation(session, conversation.id) == primary
    _ = creator


@pytest.mark.asyncio
async def test_space_send_follows_route_and_rejects_platform_mismatch(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c22-space")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_canonical_presence_preconfig(session, "8000")
        presence_b = await ensure_canonical_presence_preconfig(session, "8001")
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=str(uuid4()),
            kind="space",
            owner_id=space,
            scope_key="bot:8000:group:2001",
        )
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_space(space) == "taken"
    row = await _service(database).create(
        _script(group_id="$current_group_id"),
        inbound=_inbound("9000", group_id="2001"),
        conversation_key="group:2001",
    )
    assert row.canonical_target_space_id == space
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)
    executor = AutomationExecutor(
        settings=settings,
        registry=_send_registry(),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=lambda context: OneBotProactiveGateway(
            bot_user_id=context.bot_user_id,
            creator_user_id=context.creator_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            ledger=EventLedgerRepository(database),
            actions=AgentActionRepository(database),
            registry=registry,
            router=router,
            target_person_id=context.canonical_target_person_id,
            target_space_id=context.canonical_target_space_id,
        ),
        router=router,
    )
    first_run = await _new_run(repository, row.id, clock)
    first = await executor.execute(row, first_run)
    assert first.status is RunStatus.SUCCEEDED
    assert bot_a.calls and bot_a.calls[0][0] == "send_group_msg"
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    assert await router.cas_takeover_space(space) == "taken"
    same = await repository.get(row.id)
    assert same is not None
    second_run = await _new_run(repository, same.id, clock)
    second = await executor.execute(same, second_run)
    assert second.status is RunStatus.SUCCEEDED
    assert bot_b.calls
    async with database.sessions() as session, session.begin():
        await session.execute(text("DROP TRIGGER IF EXISTS trg_presences_route_consistency_update"))
        presence = await session.get(PresenceModel, presence_b)
        assert presence is not None
        presence.platform = "telegram"
    latest = await repository.get(row.id)
    assert latest is not None
    blocked_run = await _new_run(repository, latest.id, clock)
    blocked = await executor.execute(latest, blocked_run)
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.messages_sent == 0
    assert len(bot_b.calls) == 1


@pytest.mark.asyncio
async def test_fail_closed_routes_never_send(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c22-fail")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_canonical_presence_preconfig(session, "8000")
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    assert await router.cas_takeover_person(person) == "taken"
    row = await _service(database).create(
        _script(),
        inbound=_inbound("9000"),
        conversation_key="private:9000",
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)

    def factory(context):
        return OneBotProactiveGateway(
            bot_user_id=context.bot_user_id,
            creator_user_id=context.creator_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            ledger=EventLedgerRepository(database),
            actions=AgentActionRepository(database),
            registry=registry,
            router=router,
            target_person_id=context.canonical_target_person_id,
            target_space_id=context.canonical_target_space_id,
        )

    executor = AutomationExecutor(
        settings=settings,
        registry=_send_registry(),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=factory,
        router=router,
    )

    async def _run() -> object:
        current = await repository.get(row.id)
        assert current is not None
        run = await _new_run(repository, current.id, clock)
        return await executor.execute(current, run)

    async with database.sessions() as session, session.begin():
        route = await session.get(PersonActiveRouteModel, person)
        assert route is not None
        route.paused = True
    paused = await _run()
    assert paused.status is RunStatus.BLOCKED
    assert paused.messages_sent == 0
    assert bot.calls == []

    async with database.sessions() as session, session.begin():
        route = await session.get(PersonActiveRouteModel, person)
        assert route is not None
        route.paused = False
        owned = await session.get(PresenceModel, presence)
        assert owned is not None
        owned.enabled = False
    disabled_presence = await _run()
    assert disabled_presence.status is RunStatus.BLOCKED
    assert bot.calls == []

    async with database.sessions() as session, session.begin():
        owned = await session.get(PresenceModel, presence)
        assert owned is not None
        owned.enabled = True
    registry.disconnect(bot)
    disconnected = await _run()
    assert disconnected.status is RunStatus.BLOCKED
    assert bot.calls == []

    unpinned = napcat_registry(gateway_instance_id="gw-c22-unpin")
    left = _Bot("8000")
    right = _Bot("8000")
    unpinned.connect(left)
    unpinned.connect(right)
    unpinned.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    with pytest.raises(RouteSendError) as ambiguous:
        await PresenceRouter(database, unpinned, membership_probe=_true).resolve_send_for_person(
            person
        )
    assert ambiguous.value.category == "ambiguous"
    assert bot.calls == []

    bare = napcat_registry(gateway_instance_id="gw-c22-none")
    empty_router = PresenceRouter(database, bare, membership_probe=_true)
    async with database.sessions() as session, session.begin():
        await session.delete(await session.get(PersonActiveRouteModel, person))
    with pytest.raises(RouteSendError):
        await empty_router.resolve_send_for_person(person)


@pytest.mark.asyncio
async def test_multi_binding_history_stays_on_primary_alias(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    conversation_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_canonical_presence_preconfig(session, "8000")
        presence_b = await ensure_canonical_presence_preconfig(session, "8001")
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=conversation_id,
            kind="private",
            owner_id=person,
            scope_key="bot:8000:private:9000",
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="hist-a",
                scope_type="private",
                private_peer_user_id="9000",
                sender_user_id="9000",
                direction="inbound",
                event_kind="message",
                content="来自绑定A",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                author_kind=AuthorKind.PERSON.value,
                author_person_id=person,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                suppression_status="keeper",
            )
        )
    captured: dict[str, object] = {}

    async def generate(arguments, context):
        captured["key"] = context.conversation_key
        captured["cid"] = context.canonical_conversation_id
        rows = await EventLedgerRepository(database).list_canonical_recent(
            context.canonical_conversation_id, limit=10
        )
        captured["history"] = [row.content for row in rows]
        return CapabilityResult(data={"text": "ok"}, llm_calls=1, tool_calls=0)

    registry = napcat_registry(gateway_instance_id="gw-c22-hist")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_person(person) == "taken"
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.identity.db_models import IdentityBindingModel

        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person,
                platform=IDENTITY_PLATFORM,
                external_account_id="9001",
                display_name="alias",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ChatEventModel(
                bot_user_id="8001",
                platform_message_id="hist-b",
                scope_type="private",
                private_peer_user_id="9001",
                sender_user_id="9001",
                direction="inbound",
                event_kind="message",
                content="来自绑定B",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                author_kind=AuthorKind.PERSON.value,
                author_person_id=person,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                suppression_status="keeper",
            )
        )
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "读历史",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "none", "history_limit": 8},
            "steps": [
                {
                    "id": "gen",
                    "call": "yuki.generate",
                    "arguments": {"instruction": "看", "context_profile": "none"},
                }
            ],
            "limits": {
                "max_steps": 1,
                "max_llm_calls": 1,
                "max_tool_calls": 1,
                "max_messages": 1,
                "timeout_seconds": 30,
            },
        }
    )
    row = await _service(database).create(
        script, inbound=_inbound("9000"), conversation_key="private:9000"
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)
    executor = AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        router=router,
    )
    run = await _new_run(repository, row.id, clock)
    result = await executor.execute(row, run)
    assert result.status is RunStatus.SUCCEEDED
    assert captured["key"] == "bot:8000:private:9000"
    assert captured["cid"] == conversation_id
    assert "来自绑定A" in captured["history"]
    assert "来自绑定B" in captured["history"]
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    same = await repository.get(row.id)
    assert same is not None
    again = await _new_run(repository, same.id, clock)
    await executor.execute(same, again)
    assert captured["key"] == "bot:8000:private:9000"
    assert captured["cid"] == conversation_id
    async with database.sessions() as session:
        conversation = await conversation_for_owner(session, kind="private", person_id=person)
        assert conversation is not None
        assert conversation.id == conversation_id
        assert conversation.generation == 1
        assert await primary_alias_for_conversation(session, conversation.id) == (
            "bot:8000:private:9000"
        )


@pytest.mark.asyncio
async def test_author_kind_four_states_and_automation_is_origin(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c22-author")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_canonical_presence_preconfig(session, "8000")
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=str(uuid4()),
            kind="private",
            owner_id=person,
            scope_key="bot:8000:private:9000",
        )
        for kind, message_id in (
            (AuthorKind.PERSON.value, "seed-person"),
            (AuthorKind.EXTERNAL_BOT.value, "seed-bot"),
            (AuthorKind.SYSTEM.value, "seed-system"),
        ):
            session.add(
                ChatEventModel(
                    bot_user_id="8000",
                    platform_message_id=message_id,
                    scope_type="private",
                    private_peer_user_id="9000",
                    sender_user_id="9000" if kind == AuthorKind.PERSON.value else "8000",
                    direction="inbound",
                    event_kind="message",
                    content=kind,
                    visual_summary="",
                    segments_json="[]",
                    origin="user_message",
                    occurred_at=_NOW,
                    observed_at=_NOW,
                    author_kind=kind,
                    author_person_id=person if kind == AuthorKind.PERSON.value else None,
                    canonical_event_id=str(uuid4()),
                )
            )
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    assert await router.cas_takeover_person(person) == "taken"
    row = await _service(database).create(
        _script(),
        inbound=_inbound("9000"),
        conversation_key="private:9000",
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)
    result = await AutomationExecutor(
        settings=settings,
        registry=_send_registry(),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=lambda context: OneBotProactiveGateway(
            bot_user_id=context.bot_user_id,
            creator_user_id=context.creator_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            ledger=EventLedgerRepository(database),
            actions=AgentActionRepository(database),
            registry=registry,
            router=router,
            target_person_id=context.canonical_target_person_id,
            target_space_id=context.canonical_target_space_id,
        ),
        router=router,
    ).execute(row, await _new_run(repository, row.id, clock))
    assert result.status is RunStatus.SUCCEEDED
    async with database.sessions() as session:
        kinds = set(await session.scalars(select(ChatEventModel.author_kind).distinct()))
        origins = set(await session.scalars(select(ChatEventModel.origin).distinct()))
        automations = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.origin == "scheduled_automation")
            )
        )
    assert AuthorKind.YUKI.value in kinds
    assert kinds <= EVENT_AUTHOR_KINDS
    assert "automation" not in kinds
    assert "scheduled_automation" in origins
    assert automations
    assert all(row.author_kind == AuthorKind.YUKI.value for row in automations)
    assert all(row.author_presence_id == presence for row in automations)
    assert all(row.bot_user_id == "8000" for row in automations)


@pytest.mark.asyncio
async def test_revoked_grant_and_disabled_person_do_not_execute(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    sent: list[object] = []

    async def send(arguments, context):
        sent.append(arguments)
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    row = await _service(database).create(
        _script(),
        inbound=_inbound("9000"),
        conversation_key="private:9000",
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    repository = AutomationRepository(database)
    revoked = make_settings(database.url, automation_enabled=True, superusers_csv="")
    run = await _new_run(repository, row.id, clock)
    blocked = await AutomationExecutor(
        settings=revoked,
        registry=build_capability_registry({"onebot.send_private_message": send}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, run)
    assert blocked.status is RunStatus.BLOCKED
    assert sent == []
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.identity.db_models import CanonicalPersonModel

        target = await session.get(CanonicalPersonModel, person)
        assert target is not None
        target.enabled = False
    live = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    current = await repository.get(row.id)
    assert current is not None
    again = await _new_run(repository, current.id, clock)
    disabled = await AutomationExecutor(
        settings=live,
        registry=build_capability_registry({"onebot.send_private_message": send}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(current, again)
    assert disabled.status is RunStatus.BLOCKED
    assert disabled.error_category == "target_disabled"
    assert sent == []


@pytest.mark.asyncio
async def test_forget_person_drops_target_and_created_tasks(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        creator_q = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "10001", now=_NOW)
        other = await ensure_canonical_person_preconfig(session, "10003", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    person_task = await _service(database).create(
        _script(user_id="10001"),
        inbound=_inbound("9000", "10001"),
        conversation_key="private:9000",
    )
    p_space = await _service(database).create(
        _script(group_id="$current_group_id"),
        inbound=_inbound("10001", group_id="2001"),
        conversation_key="group:2001",
    )
    q_space = await _service(database).create(
        _script(group_id="$current_group_id"),
        inbound=_inbound("9000", group_id="2001"),
        conversation_key="group:2001",
    )
    other_task = await _service(database).create(
        _script(user_id="10003"),
        inbound=_inbound("9000", "10003"),
        conversation_key="private:9000",
    )
    assert person_task.canonical_target_person_id == person
    assert p_space.canonical_creator_person_id == person
    assert q_space.canonical_target_space_id == space
    assert q_space.canonical_creator_person_id == creator_q
    assert other_task.canonical_target_person_id == other
    assert await PeopleRepository(database).delete_person("10001") is True
    async with database.sessions() as session:
        leftover = list(await session.scalars(select(AutomationModel)))
        ids = {int(row.id) for row in leftover}
        assert person_task.id not in ids
        assert p_space.id not in ids
        assert q_space.id in ids
        assert other_task.id in ids
        kept_space = await session.get(AutomationModel, q_space.id)
        assert kept_space is not None
        assert kept_space.canonical_target_space_id == space
        assert kept_space.canonical_creator_person_id == creator_q
        assert kept_space.creator_user_id != "10001"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AutomationModel)
                .where(
                    or_(
                        AutomationModel.canonical_target_person_id == person,
                        AutomationModel.canonical_creator_person_id == person,
                        AutomationModel.creator_user_id == "10001",
                    )
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_control_projection_shows_target_without_external_id(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    row = await _service(database).create(
        _script(),
        inbound=_inbound("9000"),
        conversation_key="private:9000",
    )
    listed = await _query(database).list_automations(
        _context(_principal("control.automation.read")),
        PageRequest(limit=20),
    )
    assert listed.items
    view = next(item for item in listed.items if item.automation_id == row.id)
    assert view.target_kind == "person"
    assert view.target_id == person
    assert view.route_state in {"configured", "paused", "missing"}
    dumped = json.dumps(dataclasses.asdict(view))
    assert "1001" not in dumped
    assert "9000" not in dumped


def test_c6_backfill_fills_creator_on_0047_fixture(tmp_path: Path) -> None:
    path = tmp_path / "c6-creator.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        auto_id = _insert_legacy_automation(
            connection,
            creator="1001",
            script={
                "steps": [
                    {
                        "call": "onebot.send_private_message",
                        "arguments": {"user_id": "1001"},
                    }
                ]
            },
            authority={"creator_user_id": "1001"},
        )
        assert (
            connection.execute(
                "SELECT canonical_creator_person_id FROM automations WHERE id = ?",
                (auto_id,),
            ).fetchone()[0]
            is None
        )
        connection.commit()
    report = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert report.status == "succeeded", report.conflicts
    with _open(path) as connection:
        creator = connection.execute(
            "SELECT canonical_creator_person_id FROM automations WHERE id = ?",
            (auto_id,),
        ).fetchone()[0]
        owner = connection.execute(
            "SELECT person_id FROM identity_bindings WHERE external_account_id = '1001'"
        ).fetchone()[0]
        assert creator == owner
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] >= 1


@pytest.mark.asyncio
async def test_person_principal_follows_new_binding_not_raw_qq(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    sent: list[object] = []

    async def generate(arguments, context):
        sent.append(context.authority.actor_is_superuser)
        return CapabilityResult(data={"text": "ok"}, llm_calls=1)

    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    row = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    async with database.sessions() as session, session.begin():
        original = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.person_id == person,
                IdentityBindingModel.external_account_id == "9000",
            )
        )
        assert original is not None
        original.status = "disabled"
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person,
                platform=IDENTITY_PLATFORM,
                external_account_id="9001",
                display_name="new",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9001")
    result = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=AutomationRepository(database),
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, await _new_run(AutomationRepository(database), row.id, clock))
    assert result.status is RunStatus.SUCCEEDED
    assert sent == [True]


@pytest.mark.asyncio
async def test_foreign_person_with_old_raw_qq_does_not_inherit(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    sent: list[object] = []

    async def generate(arguments, context):
        sent.append(True)
        return CapabilityResult(data={"text": "ok"}, llm_calls=1)

    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    row = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    async with database.sessions() as session, session.begin():
        stolen = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.person_id == person,
                IdentityBindingModel.external_account_id == "9000",
            )
        )
        assert stolen is not None
        await session.delete(stolen)
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person,
                platform=IDENTITY_PLATFORM,
                external_account_id="9001",
                display_name="kept",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    blocked = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=AutomationRepository(database),
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, await _new_run(AutomationRepository(database), row.id, clock))
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error_category == "delegated_authority_revoked"
    assert sent == []


@pytest.mark.asyncio
async def test_creator_null_or_inactive_bindings_block(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    sent: list[object] = []

    async def generate(arguments, context):
        sent.append(True)
        return CapabilityResult(data={"text": "ok"}, llm_calls=1)

    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    row = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_creator_person_id = None
    stale_null = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, await _new_run(repository, row.id, clock))
    assert stale_null.status is RunStatus.BLOCKED
    assert stale_null.error_category == "state_mismatch"
    assert stale_null.steps_completed == 0
    assert sent == []
    matching_null = await repository.get(row.id)
    assert matching_null is not None
    missing = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(matching_null, await _new_run(repository, row.id, clock))
    assert missing.status is RunStatus.BLOCKED
    assert missing.error_category == "target_missing"
    assert sent == []
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_creator_person_id = person
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person)
        )
        assert binding is not None
        binding.status = "disabled"
    inactive = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, await _new_run(repository, row.id, clock))
    assert inactive.status is RunStatus.BLOCKED
    assert inactive.error_category == "delegated_authority_revoked"
    assert sent == []


@pytest.mark.asyncio
async def test_stale_claimed_record_blocks_after_db_mutation(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    sent: list[object] = []

    async def generate(arguments, context):
        sent.append(True)
        return CapabilityResult(data={"text": "ok"}, llm_calls=1)

    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        other = await ensure_canonical_person_preconfig(session, "10001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    row = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    doomed = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)
    executor = AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    )

    async def execute_stale() -> object:
        return await executor.execute(row, await _new_run(repository, row.id, clock))

    async def restore_identity() -> None:
        async with database.sessions() as session, session.begin():
            stored = await session.get(AutomationModel, row.id)
            assert stored is not None
            stored.canonical_creator_person_id = person
            stored.canonical_target_person_id = person
            stored.canonical_target_space_id = None
            stored.status = "active"

    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_creator_person_id = None
    creator_null = await execute_stale()
    assert creator_null.status is RunStatus.BLOCKED
    assert creator_null.error_category == "state_mismatch"
    assert creator_null.steps_completed == 0
    assert sent == []

    await restore_identity()
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_creator_person_id = other
    creator_changed = await execute_stale()
    assert creator_changed.status is RunStatus.BLOCKED
    assert creator_changed.error_category == "state_mismatch"
    assert creator_changed.steps_completed == 0
    assert sent == []

    await restore_identity()
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_target_person_id = None
        stored.canonical_target_space_id = None
    target_null = await execute_stale()
    assert target_null.status is RunStatus.BLOCKED
    assert target_null.error_category == "state_mismatch"
    assert target_null.steps_completed == 0
    assert sent == []

    await restore_identity()
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_target_person_id = other
    target_changed = await execute_stale()
    assert target_changed.status is RunStatus.BLOCKED
    assert target_changed.error_category == "state_mismatch"
    assert target_changed.steps_completed == 0
    assert sent == []

    await restore_identity()
    async with database.sessions() as session, session.begin():
        await session.execute(
            text("DROP TRIGGER IF EXISTS trg_automations_extension_shadow_update")
        )
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_target_person_id = person
        stored.canonical_target_space_id = space
    double_target = await execute_stale()
    assert double_target.status is RunStatus.BLOCKED
    assert double_target.error_category == "state_mismatch"
    assert double_target.steps_completed == 0
    assert sent == []

    await restore_identity()
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.status = "paused"
    paused = await execute_stale()
    assert paused.status is RunStatus.BLOCKED
    assert paused.error_category == "automation_inactive"
    assert paused.steps_completed == 0
    assert sent == []

    await restore_identity()
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.status = "cancelled"
    disabled = await execute_stale()
    assert disabled.status is RunStatus.BLOCKED
    assert disabled.error_category == "automation_inactive"
    assert disabled.steps_completed == 0
    assert sent == []

    await restore_identity()
    doomed_run = await _new_run(repository, doomed.id, clock)
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, doomed.id)
        assert stored is not None
        await session.delete(stored)
    missing_row = await executor.execute(doomed, doomed_run)
    assert missing_row.status is RunStatus.BLOCKED
    assert missing_row.error_category == "automation_inactive"
    assert missing_row.steps_completed == 0
    assert sent == []

    async with database.sessions() as session, session.begin():
        original = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.person_id == person,
                IdentityBindingModel.external_account_id == "9000",
            )
        )
        assert original is not None
        original.status = "disabled"
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person,
                platform=IDENTITY_PLATFORM,
                external_account_id="9001",
                display_name="replacement",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    rebound = await AutomationExecutor(
        settings=make_settings(database.url, automation_enabled=True, superusers_csv="9001"),
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, await _new_run(repository, row.id, clock))
    assert rebound.status is RunStatus.SUCCEEDED
    assert sent == [True]


@pytest.mark.asyncio
async def test_v2_null_target_or_missing_router_never_sends(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c22-legacy")
    bot = _Bot("8000")
    sent: list[object] = []

    async def send(arguments, context):
        sent.append(arguments)
        if context.gateway is not None:
            await context.gateway.send_private("9000", "测")
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    registry.connect(bot)
    row = await _service(database).create(
        _script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    repository = AutomationRepository(database)

    def factory(context):
        return OneBotProactiveGateway(
            bot_user_id=context.bot_user_id,
            creator_user_id=context.creator_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            ledger=EventLedgerRepository(database),
            actions=AgentActionRepository(database),
            registry=registry,
            router=None,
            target_person_id=context.canonical_target_person_id,
            target_space_id=context.canonical_target_space_id,
        )

    no_router = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"onebot.send_private_message": send}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=factory,
    ).execute(row, await _new_run(repository, row.id, clock))
    assert no_router.status is RunStatus.BLOCKED
    assert no_router.error_category == "operation_unavailable"
    assert sent == []
    assert bot.calls == []
    gateway = OneBotProactiveGateway(
        bot_user_id="8000",
        creator_user_id="9000",
        automation_id=row.id,
        automation_run_id=1,
        ledger=EventLedgerRepository(database),
        actions=AgentActionRepository(database),
        registry=registry,
        router=None,
        target_person_id=person,
    )
    with pytest.raises(Exception) as missing_router:
        await gateway.send_private("9000", "测")
    assert getattr(missing_router.value, "category", "") == "none"
    assert bot.calls == []
    async with database.sessions() as session, session.begin():
        stored = await session.get(AutomationModel, row.id)
        assert stored is not None
        stored.canonical_target_person_id = None
        stored.canonical_target_space_id = None
    current = await repository.get(row.id)
    assert current is not None
    null_target = await AutomationExecutor(
        settings=settings,
        registry=build_capability_registry({"onebot.send_private_message": send}),
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=factory,
        router=None,
    ).execute(current, await _new_run(repository, current.id, clock))
    assert null_target.status is RunStatus.BLOCKED
    assert null_target.error_category == "target_missing"
    assert sent == []
    assert bot.calls == []


@pytest.mark.asyncio
async def test_existing_conversation_without_primary_alias_blocks(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    conversation_id = str(uuid4())
    ran = False

    async def generate(arguments, context):
        nonlocal ran
        ran = True
        return CapabilityResult(data={"text": "ok"}, llm_calls=1)

    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=conversation_id,
            kind="private",
            owner_id=person,
            scope_key="bot:8000:private:9000",
        )
    row = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    async with database.sessions() as session, session.begin():
        await session.execute(text("PRAGMA foreign_keys=OFF"))
        await session.execute(
            text("DROP TRIGGER IF EXISTS trg_conversation_legacy_aliases_primary_update")
        )
        alias = await session.scalar(
            select(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.conversation_id == conversation_id
            )
        )
        assert alias is not None
        alias.is_primary = 0
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    blocked = await AutomationExecutor(
        settings=make_settings(database.url, automation_enabled=True, superusers_csv="9000"),
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=AutomationRepository(database),
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, await _new_run(AutomationRepository(database), row.id, clock))
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error_category == "state_mismatch"
    assert blocked.steps_completed == 0
    assert ran is False


@pytest.mark.asyncio
async def test_dual_filled_target_blocks_even_if_trigger_bypassed(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    sent: list[object] = []

    async def generate(arguments, context):
        sent.append(True)
        return CapabilityResult(data={"text": "ok"}, llm_calls=1)

    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    row = await _service(database).create(
        _generate_script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    dual = row.model_copy(update={"canonical_target_space_id": space})
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    blocked = await AutomationExecutor(
        settings=make_settings(database.url, automation_enabled=True, superusers_csv="9000"),
        registry=build_capability_registry({"yuki.generate": generate}),
        repository=AutomationRepository(database),
        time_service=TimeContextService(database, clock=clock),
    ).execute(dual, await _new_run(AutomationRepository(database), row.id, clock))
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error_category == "state_mismatch"
    assert sent == []


@pytest.mark.asyncio
async def test_pinned_multi_connection_still_sends(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-c22-pin")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    extra = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_canonical_presence_preconfig(session, "8000")
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    row = await _service(database).create(
        _script(), inbound=_inbound("9000"), conversation_key="private:9000"
    )
    async with database.sessions() as session:
        person = row.canonical_target_person_id
        assert person is not None
    assert await router.cas_takeover_person(person) == "taken"
    registry.connect(extra)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    result = await AutomationExecutor(
        settings=make_settings(database.url, automation_enabled=True, superusers_csv="9000"),
        registry=_send_registry(),
        repository=AutomationRepository(database),
        time_service=TimeContextService(database, clock=clock),
        gateway_factory=lambda context: OneBotProactiveGateway(
            bot_user_id=context.bot_user_id,
            creator_user_id=context.creator_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            ledger=EventLedgerRepository(database),
            actions=AgentActionRepository(database),
            registry=registry,
            router=router,
            target_person_id=context.canonical_target_person_id,
            target_space_id=context.canonical_target_space_id,
        ),
        router=router,
    ).execute(row, await _new_run(AutomationRepository(database), row.id, clock))
    assert result.status is RunStatus.SUCCEEDED
    assert len(bot.calls) == 1
    assert extra.calls == []


def test_ast_gate_forbids_get_bots_raw_keys_admin_and_connection_persist() -> None:
    forbidden = {
        "get_bots",
        "AdminActor",
        "GatewayConnection",
        "gateway_connections",
    }
    hits: list[tuple[str, int, str]] = []
    for path in _AUTOMATION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = ""
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            if name in forbidden:
                hits.append((path.as_posix(), getattr(node, "lineno", 0), name))
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "is_superuser"
                and isinstance(node.ctx, ast.Store)
            ):
                hits.append((path.as_posix(), node.lineno, "is_superuser"))
    assert hits == []

    routing = ast.parse((_SRC / "identity" / "routing.py").read_text(encoding="utf-8"))
    for node in ast.walk(routing):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get_bots":
            raise AssertionError("routing must not call get_bots")
