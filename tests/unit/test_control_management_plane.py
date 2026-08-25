"""C13-C15: real config, memory, and remaining management-plane semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.conftest import make_settings

from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.control_plane import (
    CONTROL_CAPABILITY_IDS,
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlPrincipal,
    ControlQueryError,
    ControlQueryService,
    DecisionContext,
    PageRequest,
    PrincipalSource,
    ProblemCode,
    YukiControlTarget,
    is_forbidden_control_capability,
    is_protocol_capability,
)
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId
from qq_ai_bot.emoji.db_models import EmojiAssetModel
from qq_ai_bot.health import HealthPayload
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.dream.service import DreamService, plan_full_core
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.models import EmbeddingVector
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_management import state_revision
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    MCPServerStateModel,
    MemoryEmbeddingModel,
    MemoryRebuildRunModel,
    RuntimeConfigOverrideModel,
)
from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository
from qq_ai_bot.persistence.repositories import UserProfileRepository
from qq_ai_bot.persistence.unit_of_work import next_updated_at
from qq_ai_bot.plugin_host.db_models import PluginNotificationOutboxModel
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.speech.db_models import SpeechVoiceProfileModel
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
_CONFIG_KEY = "memory.retrieval_enabled"
_GLOBAL_ONLY_KEY = "memory.intent_rerank_enabled"
_SECRET_KEY = "llm.api_key"
_MIN_KEY = "reply.delay_min_seconds"
_WRITE_CAPS = (
    "control.config.mutate",
    "control.memory.mutate",
    "control.memory.rebuild",
    "control.memory.dream",
    "control.memory.maintenance",
    "control.automation.mutate",
    "control.plugin.mutate",
    "control.mcp.mutate",
    "control.emoji.mutate",
    "control.speech.mutate",
    "control.operation.cancel",
    "control.operation.retry",
)
_READ_CAPS = (
    "control.config.read",
    "control.audit.read",
    "control.memory.metadata.read",
    "control.memory.content.read",
    "control.automation.read",
    "control.plugin.read",
    "control.mcp.read",
    "control.emoji.read",
    "control.speech.read",
    "control.health.read",
    "control.operation.read",
)


def _settings(database: Database, **overrides: object):
    values = {
        "memory_rebuild_enabled": True,
        "automation_enabled": True,
        "memory_maintenance_enabled": True,
        "plugin_system_enabled": True,
    }
    values.update(overrides)
    return make_settings(database.url, **values)


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
    *,
    request_id: RequestId | None = None,
) -> DecisionContext[ControlPrincipal, PrincipalSource, YukiControlTarget]:
    return DecisionContext(
        request_id=request_id or RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=YukiControlTarget.PERMANENT_YUKI,
        reason="management",
    )


def _command(
    request_id: RequestId,
    *,
    expected_revision: int,
    payload: object,
) -> ControlCommand:
    return ControlCommand(
        request_id=request_id,
        expected_revision=expected_revision,
        payload=payload,
    )


def _commands(
    database: Database,
    *,
    mcp: MCPManager | None = None,
    embeddings: object | None = None,
    **setting_overrides: object,
) -> ControlCommandService:
    if embeddings is not None:
        setting_overrides.setdefault("memory_embedding_enabled", True)
        setting_overrides.setdefault("memory_embedding_base_url", "http://127.0.0.1")
        setting_overrides.setdefault("memory_embedding_api_key", "test-embedding")
    return ControlCommandService(
        ControlCommandAdapter(
            database,
            settings=_settings(database, **setting_overrides),
            mcp_manager=mcp,
            embeddings=embeddings,
        )
    )


class _PlanningEmbeddings:
    def __init__(self, profile_id: int, documents: EmbeddingDocumentBuilder) -> None:
        self.profile_id = profile_id
        self.dimensions = 1024
        self.documents = documents
        self.jobs = None


async def _planning_embeddings(database: Database) -> _PlanningEmbeddings:
    provider = FakeEmbeddingProvider(dimensions=1024)
    profile = await MemoryEmbeddingRepository(database).ensure_profile(provider.profile)
    return _PlanningEmbeddings(
        profile.id,
        EmbeddingDocumentBuilder(template_version=1, max_characters=4000),
    )


def _unit_vector() -> EmbeddingVector:
    return EmbeddingVector(values=(1.0,) + (0.0,) * 1023, dimensions=1024)


async def _rewrite_success_status(database: Database, request_id: str, status: str) -> None:
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == request_id
            )
        )
        assert receipt is not None
        assert receipt.effective_state_json is not None
        state = json.loads(receipt.effective_state_json)
        state["status"] = status
        receipt.effective_state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        audit = await session.get(AdminOperationEventModel, receipt.audit_id)
        assert audit is not None
        after = json.loads(audit.after_json)
        after["status"] = status
        audit.after_json = json.dumps(after, ensure_ascii=False, separators=(",", ":"))


def _queries(database: Database, *, mcp: MCPManager | None = None) -> ControlQueryService:
    return ControlQueryService(
        ControlQueryAdapter(database, settings=_settings(database), mcp_manager=mcp)
    )


async def _runtime_change_id(database: Database, *, operation: str = "set_override") -> int:
    async with database.sessions() as session:
        row = await session.scalar(
            select(AdminOperationEventModel)
            .where(AdminOperationEventModel.capability == "runtime_config")
            .where(AdminOperationEventModel.operation == operation)
            .where(AdminOperationEventModel.success.is_(True))
            .order_by(AdminOperationEventModel.id.desc())
        )
    assert row is not None
    return int(row.id)


def test_runtime_config_service_has_no_forged_actor_path() -> None:
    source = (SRC_ROOT / "qq_ai_bot" / "admin" / "config_service.py").read_text(encoding="utf-8")
    assert "def _actor" not in source
    assert "AdminActor(" not in source
    assert "is_superuser=" not in source
    assert "is_superuser=True" not in source


def test_dangerous_management_capabilities_stay_absent_and_web_search_is_legal() -> None:
    for capability in (
        "mcp.call",
        "mcp.tool.call",
        "plugin.run",
        "plugin.arbitrary.run",
        "call_onebot_api",
        "raw_sql",
        "secret.read",
    ):
        assert capability not in CONTROL_CAPABILITY_IDS
        assert is_forbidden_control_capability(capability)
    assert "web_search" not in CONTROL_CAPABILITY_IDS
    assert is_forbidden_control_capability("web_search") is False
    assert is_forbidden_control_capability("mcp.web_search") is False
    assert is_protocol_capability("web_search") is True
    assert is_protocol_capability("mcp.web_search") is True


def test_public_healthz_payload_keys_stay_thin() -> None:
    keys = set(get_type_hints(HealthPayload))
    assert "status" in keys
    assert "version" in keys
    assert "database" in keys
    assert "onebot_connected" in keys
    assert "webui_token" not in keys
    assert "api_key" not in keys


@pytest.mark.asyncio
async def test_bare_adapter_keeps_management_unavailable(database: Database) -> None:
    service = ControlCommandService(ControlCommandAdapter(database))
    context = _context(_principal("control.config.mutate"))
    with pytest.raises(ControlCommandError) as denied:
        await service.set_config(
            context,
            _command(
                context.request_id,
                expected_revision=0,
                payload={
                    "key": _CONFIG_KEY,
                    "scope_type": "global",
                    "scope_id": "",
                    "value": False,
                },
            ),
        )
    assert denied.value.problem.code is ProblemCode.OPERATION_UNAVAILABLE


@pytest.mark.asyncio
async def test_config_set_cas_secret_and_rollback_restores_prior_value(
    database: Database,
) -> None:
    service = _commands(database)
    queries = _queries(database)
    principal = _principal(*_WRITE_CAPS, *_READ_CAPS)
    wrong = _context(principal)
    with pytest.raises(ControlCommandError) as conflict:
        await service.set_config(
            wrong,
            _command(
                wrong.request_id,
                expected_revision=7,
                payload={
                    "key": _CONFIG_KEY,
                    "scope_type": "global",
                    "scope_id": "",
                    "value": False,
                },
            ),
        )
    assert conflict.value.problem.code is ProblemCode.VERSION_CONFLICT

    secret_ctx = _context(principal)
    with pytest.raises(ControlCommandError) as secret:
        await service.set_config(
            secret_ctx,
            _command(
                secret_ctx.request_id,
                expected_revision=0,
                payload={
                    "key": _SECRET_KEY,
                    "scope_type": "global",
                    "scope_id": "",
                    "value": "sk-forged",
                },
            ),
        )
    assert secret.value.problem.code is ProblemCode.SECRET_NOT_READABLE

    first_ctx = _context(principal)
    first = await service.set_config(
        first_ctx,
        _command(
            first_ctx.request_id,
            expected_revision=0,
            payload={
                "key": _CONFIG_KEY,
                "scope_type": "global",
                "scope_id": "",
                "value": False,
            },
        ),
    )
    assert first.success is True
    assert first.revision == 1
    change_a = await _runtime_change_id(database)
    replayed = await service.set_config(
        first_ctx,
        _command(
            first_ctx.request_id,
            expected_revision=0,
            payload={
                "key": _CONFIG_KEY,
                "scope_type": "global",
                "scope_id": "",
                "value": False,
            },
        ),
    )
    assert replayed.audit_id == first.audit_id
    second_ctx = _context(principal)
    second = await service.set_config(
        second_ctx,
        _command(
            second_ctx.request_id,
            expected_revision=1,
            payload={
                "key": _CONFIG_KEY,
                "scope_type": "global",
                "scope_id": "",
                "value": True,
            },
        ),
    )
    assert second.revision == 2
    change_b = await _runtime_change_id(database)
    assert change_b != change_a
    assert change_b != int(second.audit_id)

    rollback_ctx = _context(principal)
    rolled = await service.rollback_config(
        rollback_ctx,
        _command(
            rollback_ctx.request_id,
            expected_revision=2,
            payload={"change_id": change_b},
        ),
    )
    assert rolled.success is True
    async with database.sessions() as session:
        override = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.config_key == _CONFIG_KEY,
                RuntimeConfigOverrideModel.scope_type == "global",
                RuntimeConfigOverrideModel.scope_id == "",
            )
        )
        audit = await session.get(AdminOperationEventModel, int(first.audit_id))
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.audit_id == int(first.audit_id)
            )
        )
    assert override is not None
    assert json.loads(override.value_json) is False
    assert audit is not None and audit.success is True
    assert receipt is not None
    assert '"sk-forged"' not in (audit.after_json or "")
    assert "sk-forged" not in (receipt.effective_state_json or "")

    items = []
    cursor = None
    while True:
        page = await queries.list_effective_configs(
            _context(_principal("control.config.read")),
            PageRequest(limit=50, cursor=cursor),
        )
        items.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    written = [item for item in items if item.key == _CONFIG_KEY]
    secrets = [item for item in items if item.key == _SECRET_KEY]
    assert secrets and all(item.value is None for item in secrets)
    assert written
    assert written[0].value is False


@pytest.mark.asyncio
async def test_config_scope_cross_key_and_unset(
    database: Database,
) -> None:
    commands = _commands(database)
    queries = _queries(database)
    writer = _principal("control.config.mutate", "control.audit.read")
    global_ctx = _context(writer)
    await commands.set_config(
        global_ctx,
        _command(
            global_ctx.request_id,
            expected_revision=0,
            payload={
                "key": _CONFIG_KEY,
                "scope_type": "global",
                "scope_id": "",
                "value": True,
            },
        ),
    )
    user_ctx = _context(writer)
    await commands.set_config(
        user_ctx,
        _command(
            user_ctx.request_id,
            expected_revision=0,
            payload={
                "key": _CONFIG_KEY,
                "scope_type": "user",
                "scope_id": "1001",
                "value": False,
            },
        ),
    )
    group_ctx = _context(writer)
    await commands.set_config(
        group_ctx,
        _command(
            group_ctx.request_id,
            expected_revision=0,
            payload={
                "key": _CONFIG_KEY,
                "scope_type": "group",
                "scope_id": "2001",
                "value": False,
            },
        ),
    )
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == _CONFIG_KEY
                )
            )
        )
    assert {row.scope_type for row in rows} == {"global", "user", "group"}
    unset_user = _context(writer)
    await commands.unset_config(
        unset_user,
        _command(
            unset_user.request_id,
            expected_revision=1,
            payload={"key": _CONFIG_KEY, "scope_type": "user", "scope_id": "1001"},
        ),
    )
    async with database.sessions() as session:
        remaining = list(
            await session.scalars(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == _CONFIG_KEY
                )
            )
        )
    assert {row.scope_type for row in remaining} == {"global", "group"}

    denied = _context(writer)
    with pytest.raises(ControlCommandError) as scope:
        await commands.set_config(
            denied,
            _command(
                denied.request_id,
                expected_revision=0,
                payload={
                    "key": _GLOBAL_ONLY_KEY,
                    "scope_type": "user",
                    "scope_id": "1001",
                    "value": False,
                },
            ),
        )
    assert scope.value.problem.code is ProblemCode.VALIDATION_ERROR

    invalid = _context(writer)
    with pytest.raises(ControlCommandError) as cross:
        await commands.set_config(
            invalid,
            _command(
                invalid.request_id,
                expected_revision=0,
                payload={
                    "key": _MIN_KEY,
                    "scope_type": "global",
                    "scope_id": "",
                    "value": 5,
                },
            ),
        )
    assert cross.value.problem.code is ProblemCode.VALIDATION_ERROR

    events = await queries.list_audit_events(
        _context(_principal("control.audit.read")),
        PageRequest(limit=20),
    )
    assert any(item.capability == "control.config.mutate" for item in events.items)
    with pytest.raises(ControlQueryError) as denied_query:
        await queries.list_audit_events(_context(_principal()), PageRequest(limit=20))
    assert denied_query.value.problem.code is ProblemCode.CAPABILITY_DENIED


@pytest.mark.asyncio
async def test_memory_mutate_uses_real_revision_and_creates_rebuild_run(
    database: Database,
) -> None:
    people = UserProfileRepository(database)
    await people.observe(user_id="1001", nickname="成员")
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            memory_key="favorite-food",
            category="fact",
            content="喜欢微辣土豆",
            importance=3,
            source_type=MemorySourceType.EXPLICIT,
        ),
        limit=100,
    )
    loaded = await facts.get_fact(fact.id)
    assert loaded is not None
    revision = state_revision(loaded.updated_at)
    queries = _queries(database)
    metadata = await queries.list_memory_facts(
        _context(_principal("control.memory.metadata.read")),
        PageRequest(limit=20),
    )
    assert metadata.items[0].content is None
    revealed = await queries.list_memory_facts(
        _context(_principal("control.memory.metadata.read", "control.memory.content.read")),
        PageRequest(limit=20),
    )
    assert revealed.items[0].content == "喜欢微辣土豆"

    commands = _commands(database)
    writer = _principal(
        "control.memory.mutate",
        "control.memory.rebuild",
        "control.memory.maintenance",
        "control.operation.read",
    )
    stale = _context(writer)
    with pytest.raises(ControlCommandError) as conflict:
        await commands.mutate_memory(
            stale,
            _command(
                stale.request_id,
                expected_revision=1,
                payload={"action": "confirm", "resource_id": str(fact.id)},
            ),
        )
    assert conflict.value.problem.code is ProblemCode.VERSION_CONFLICT
    first_ctx = _context(writer)
    payload = {"action": "confirm", "resource_id": str(fact.id)}
    first = await commands.mutate_memory(
        first_ctx,
        _command(first_ctx.request_id, expected_revision=revision, payload=payload),
    )
    assert first.success is True
    replayed = await commands.mutate_memory(
        first_ctx,
        _command(first_ctx.request_id, expected_revision=revision, payload=payload),
    )
    assert replayed.audit_id == first.audit_id
    rebuild_ctx = _context(writer)
    rebuild = await commands.rebuild_memory(
        rebuild_ctx,
        _command(
            rebuild_ctx.request_id,
            expected_revision=0,
            payload={"action": "start", "resource_id": "index"},
        ),
    )
    assert rebuild.success is True
    assert rebuild.operation is not None
    assert rebuild.operation.operation_id.startswith("rebuild:")
    run = await MemoryRebuildRepository(database).get_run(rebuild.resource_id)
    assert run is not None
    assert run.status.value == "extracting"
    assert rebuild.operation.status.value == "running"
    ops = await queries.list_backfill_operations(
        _context(_principal("control.operation.read")),
        PageRequest(limit=50),
    )
    assert any(item.operation.operation_id == rebuild.operation.operation_id for item in ops.items)
    assert any(item.mode == "rebuild" for item in ops.items)
    expired = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            memory_key="expired-note",
            category="fact",
            content="过期备忘",
            importance=1,
            source_type=MemorySourceType.EXPLICIT,
            valid_until=datetime.now(UTC) - timedelta(days=1),
        ),
        limit=100,
    )
    maintain_ctx = _context(writer)
    maintained = await commands.maintain_memory(
        maintain_ctx,
        _command(
            maintain_ctx.request_id,
            expected_revision=0,
            payload={"action": "run", "resource_id": "index"},
        ),
    )
    assert maintained.success is True
    assert maintained.operation is None
    current_expired = await facts.get_fact(expired.id)
    assert current_expired is not None
    assert current_expired.status is MemoryStatus.INVALIDATED


@pytest.mark.asyncio
async def test_dream_without_runtime_embeddings_is_unavailable(database: Database) -> None:
    commands = _commands(database)
    context = _context(_principal("control.memory.dream"))
    with pytest.raises(ControlCommandError) as denied:
        await commands.dream_memory(
            context,
            _command(
                context.request_id,
                expected_revision=0,
                payload={"action": "start", "resource_id": "index"},
            ),
        )
    assert denied.value.problem.code is ProblemCode.OPERATION_UNAVAILABLE


@pytest.mark.asyncio
async def test_dream_and_operations_cancel_use_real_runs(database: Database) -> None:
    embeddings = await _planning_embeddings(database)
    commands = _commands(database, embeddings=embeddings)
    queries = _queries(database)
    writer = _principal(
        "control.memory.dream",
        "control.operation.cancel",
        "control.operation.read",
    )
    dream_ctx = _context(writer)
    dreamed = await commands.dream_memory(
        dream_ctx,
        _command(
            dream_ctx.request_id,
            expected_revision=0,
            payload={"action": "start", "resource_id": "index"},
        ),
    )
    assert dreamed.operation is not None
    assert dreamed.operation.operation_id.startswith("dream:")
    run = await DreamRepository(database).get_run(dreamed.resource_id)
    assert run is not None
    assert run.status.value == "running"
    ops = await queries.list_backfill_operations(
        _context(_principal("control.operation.read")),
        PageRequest(limit=50),
    )
    assert any(item.mode == "dream" for item in ops.items)
    cancel_ctx = _context(writer)
    cancelled = await commands.cancel_operation(
        cancel_ctx,
        _command(
            cancel_ctx.request_id,
            expected_revision=state_revision(run.updated_at),
            payload={"action": "cancel", "resource_id": dreamed.operation.operation_id},
        ),
    )
    assert cancelled.success is True
    current = await DreamRepository(database).get_run(dreamed.resource_id)
    assert current is not None
    assert current.status.value == "cancelled"


@pytest.mark.asyncio
async def test_automation_plugin_emoji_speech_use_domain_services(
    database: Database,
) -> None:
    commands = _commands(database)
    queries = _queries(database)
    writer = _principal(*_WRITE_CAPS, *_READ_CAPS)
    script = {
        "version": 1,
        "name": "一次提醒",
        "timezone": "Asia/Shanghai",
        "schedule": {"type": "after", "seconds": 1},
        "context": {"scene": "none"},
        "steps": [
            {
                "id": "send",
                "call": "onebot.send_private_message",
                "arguments": {"user_id": "$creator_user_id", "text": "测试"},
            }
        ],
        "limits": {
            "max_steps": 1,
            "max_llm_calls": 0,
            "max_tool_calls": 1,
            "max_messages": 1,
            "timeout_seconds": 30,
        },
    }
    created_ctx = _context(writer)
    created = await commands.mutate_automation(
        created_ctx,
        _command(
            created_ctx.request_id,
            expected_revision=0,
            payload={"action": "create", "resource_id": "yuki", "spec": script},
        ),
    )
    assert created.success is True
    listed = await queries.list_automations(_context(writer), PageRequest(limit=20))
    assert listed.items and listed.items[0].status == "active"
    renamed = {**script, "name": "二次提醒"}
    update_ctx = _context(writer)
    updated = await commands.mutate_automation(
        update_ctx,
        _command(
            update_ctx.request_id,
            expected_revision=created.revision,
            payload={
                "action": "update",
                "resource_id": created.resource_id,
                "spec": renamed,
            },
        ),
    )
    assert updated.success is True
    listed = await queries.list_automations(_context(writer), PageRequest(limit=20))
    assert listed.items[0].name == "二次提醒"
    pause_ctx = _context(writer)
    paused = await commands.mutate_automation(
        pause_ctx,
        _command(
            pause_ctx.request_id,
            expected_revision=updated.revision,
            payload={"action": "pause", "resource_id": created.resource_id},
        ),
    )
    assert paused.effective_state["status"] == "paused"
    resume_ctx = _context(writer)
    resumed = await commands.mutate_automation(
        resume_ctx,
        _command(
            resume_ctx.request_id,
            expected_revision=paused.revision,
            payload={"action": "resume", "resource_id": created.resource_id},
        ),
    )
    assert resumed.effective_state["status"] == "active"
    run_ctx = _context(writer)
    ran = await commands.mutate_automation(
        run_ctx,
        _command(
            run_ctx.request_id,
            expected_revision=resumed.revision,
            payload={"action": "run_now", "resource_id": created.resource_id},
        ),
    )
    assert ran.success is True
    automation = await AutomationRepository(database).get(int(created.resource_id))
    assert automation is not None
    assert automation.next_run_at is not None
    run = await AutomationRepository(database).create_run(
        automation.id,
        scheduled_for=automation.next_run_at,
        actual_started_at=datetime.now(UTC),
    )
    assert run is not None
    ops = await queries.list_backfill_operations(
        _context(_principal("control.operation.read")),
        PageRequest(limit=50),
    )
    assert any(
        item.mode == "automation" and item.operation.operation_id == f"automation:{run.id}"
        for item in ops.items
    )
    cancel_ctx = _context(writer)
    cancelled = await commands.mutate_automation(
        cancel_ctx,
        _command(
            cancel_ctx.request_id,
            expected_revision=ran.revision,
            payload={"action": "cancel", "resource_id": created.resource_id},
        ),
    )
    assert cancelled.effective_state["status"] == "cancelled"
    history = await queries.list_automations(_context(writer), PageRequest(limit=20))
    assert history.items[0].status == "cancelled"
    bad_ctx = _context(writer)
    with pytest.raises(ControlCommandError) as invalid_script:
        await commands.mutate_automation(
            bad_ctx,
            _command(
                bad_ctx.request_id,
                expected_revision=0,
                payload={"action": "create", "resource_id": "yuki", "spec": {"name": "nope"}},
            ),
        )
    assert invalid_script.value.problem.code is ProblemCode.VALIDATION_ERROR

    plugins = PluginInstallationRepository(database)
    record = await plugins.upsert_discovered(
        plugin_id="demo-plugin",
        name="Demo",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish",),
    )
    enable_denied = _context(writer)
    with pytest.raises(ControlCommandError) as unapproved:
        await commands.mutate_plugin(
            enable_denied,
            _command(
                enable_denied.request_id,
                expected_revision=state_revision(record.updated_at),
                payload={"action": "enable", "resource_id": "demo-plugin"},
            ),
        )
    assert unapproved.value.problem.code is ProblemCode.PRECONDITION_FAILED
    approve_ctx = _context(writer)
    approved = await commands.mutate_plugin(
        approve_ctx,
        _command(
            approve_ctx.request_id,
            expected_revision=state_revision(record.updated_at),
            payload={"action": "approve", "resource_id": "demo-plugin"},
        ),
    )
    assert approved.success is True
    enable_ctx = _context(writer)
    enabled = await commands.mutate_plugin(
        enable_ctx,
        _command(
            enable_ctx.request_id,
            expected_revision=approved.revision,
            payload={"action": "enable", "resource_id": "demo-plugin"},
        ),
    )
    assert enabled.success is True
    doctor_ctx = _context(writer)
    doctor = await commands.mutate_plugin(
        doctor_ctx,
        _command(
            doctor_ctx.request_id,
            expected_revision=enabled.revision,
            payload={"action": "doctor", "resource_id": "demo-plugin"},
        ),
    )
    assert doctor.success is True
    assert doctor.effective_state["status"] == "unhealthy"
    installed = await queries.list_plugins(_context(writer), PageRequest(limit=20))
    assert installed.items and installed.items[0].enabled is True
    await plugins.set_status("demo-plugin", status="running")
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id="demo-plugin",
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    published = await notifications.publish(
        plugin_id="demo-plugin",
        request=PublishNotificationRequest(
            event_key="control:demo:1",
            event_type="PushEvent",
            external_source="github",
            target=target,
            occurred_at=datetime.now(UTC),
            summary="demo",
            text="通知正文",
        ),
    )
    async with database.sessions() as session:
        outbox = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.source_event_id == published.source_event_id
            )
        )
    assert outbox is not None
    retry_ctx = _context(writer)
    retried = await commands.mutate_plugin(
        retry_ctx,
        _command(
            retry_ctx.request_id,
            expected_revision=state_revision(outbox.updated_at),
            payload={"action": "retry", "resource_id": str(outbox.id)},
        ),
    )
    assert retried.success is True
    async with database.sessions() as session:
        refreshed_outbox = await session.get(PluginNotificationOutboxModel, outbox.id)
    assert refreshed_outbox is not None
    assert refreshed_outbox.status == "pending"
    retry_op = _context(writer)
    retried_op = await commands.retry_operation(
        retry_op,
        _command(
            retry_op.request_id,
            expected_revision=state_revision(refreshed_outbox.updated_at),
            payload={"action": "retry", "resource_id": f"plugin-outbox:{outbox.id}"},
        ),
    )
    assert retried_op.success is True
    outbox_ops = await queries.list_backfill_operations(
        _context(_principal("control.operation.read")),
        PageRequest(limit=50),
    )
    assert any(item.mode == "plugin-outbox" for item in outbox_ops.items)

    now = datetime.now(UTC)
    asset_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            EmojiAssetModel(
                id=asset_id,
                sha256="ab" * 32,
                relative_path=f"emoji/{asset_id}.png",
                image_format="png",
                mime_type="image/png",
                byte_size=12,
                width=16,
                height=16,
                frame_count=1,
                animated=False,
                status="candidate",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SpeechVoiceProfileModel(
                profile_id="yuki-voice",
                display_name="Yuki",
                provider="genie",
                engine_model_version="v2",
                language="zh",
                supported_languages_json='["zh"]',
                model_relative_path="voices/yuki/model",
                model_checksum="c" * 64,
                default_style="neutral",
                enabled=True,
                is_default=False,
                source="test",
                source_note="",
                license_note="",
                manifest_hash="d" * 64,
                created_at=now,
                updated_at=now,
            )
        )
    async with database.sessions() as session:
        emoji_row = await session.get(EmojiAssetModel, asset_id)
        speech_row = await session.get(SpeechVoiceProfileModel, "yuki-voice")
    assert emoji_row is not None and speech_row is not None
    pin_ctx = _context(writer)
    pin = await commands.mutate_emoji(
        pin_ctx,
        _command(
            pin_ctx.request_id,
            expected_revision=state_revision(emoji_row.updated_at),
            payload={"action": "pin", "resource_id": asset_id},
        ),
    )
    assert pin.success is True
    reject_ctx = _context(writer)
    reject = await commands.mutate_emoji(
        reject_ctx,
        _command(
            reject_ctx.request_id,
            expected_revision=pin.revision,
            payload={"action": "reject", "resource_id": asset_id},
        ),
    )
    assert reject.success is True
    speech_ctx = _context(writer)
    speech = await commands.mutate_speech(
        speech_ctx,
        _command(
            speech_ctx.request_id,
            expected_revision=state_revision(speech_row.updated_at),
            payload={"action": "disable", "resource_id": "yuki-voice"},
        ),
    )
    assert speech.success is True
    assert speech.effective_state["status"] == "disabled"
    emojis = await queries.list_emoji_assets(_context(writer), PageRequest(limit=20))
    assert emojis.items and emojis.items[0].status == "rejected"
    voices = await queries.list_speech_profiles(_context(writer), PageRequest(limit=20))
    assert voices.items and voices.items[0].enabled is False


@pytest.mark.asyncio
async def test_mcp_manager_is_used_and_arbitrary_calls_stay_closed(
    database: Database,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"search": {"command": "python", "lifecycle": "lazy"}}}),
        encoding="utf-8",
    )
    tool = SimpleNamespace(
        name="lookup",
        description="lookup",
        inputSchema={"type": "object", "properties": {}},
        outputSchema=None,
        annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
    )
    connection = FakeMCPConnection(tools=(tool,))

    class _Factory:
        def __call__(self, config: object, **_kwargs: object) -> FakeMCPConnection:
            return connection

    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=4,
        repository=MCPRepository(database),
        connection_factory=_Factory(),
    )
    await manager.start()
    commands = _commands(database, mcp=manager)
    queries = _queries(database, mcp=manager)
    page = await queries.list_mcp_servers(
        _context(_principal("control.mcp.read")),
        PageRequest(limit=20),
    )
    assert page.items
    assert page.items[0].server_id == "search"
    async with database.sessions() as session:
        state = await session.get(MCPServerStateModel, "search")
    assert state is not None
    revision = state_revision(state.updated_at)
    caller = _context(_principal("control.mcp.mutate"))
    with pytest.raises(ControlCommandError) as rejected:
        await commands.mutate_mcp(
            caller,
            _command(
                caller.request_id,
                expected_revision=revision,
                payload={"action": "call", "resource_id": "search"},
            ),
        )
    assert rejected.value.problem.code is ProblemCode.PRECONDITION_FAILED
    disable = _context(_principal("control.mcp.mutate"))
    disabled = await commands.mutate_mcp(
        disable,
        _command(
            disable.request_id,
            expected_revision=revision,
            payload={"action": "disable", "resource_id": "search"},
        ),
    )
    assert disabled.success is True
    assert disabled.effective_state["status"] == "disabled"
    enable = _context(_principal("control.mcp.mutate"))
    enabled = await commands.mutate_mcp(
        enable,
        _command(
            enable.request_id,
            expected_revision=disabled.revision,
            payload={"action": "enable", "resource_id": "search"},
        ),
    )
    assert enabled.success is True
    refresh = _context(_principal("control.mcp.mutate"))
    refreshed = await commands.mutate_mcp(
        refresh,
        _command(
            refresh.request_id,
            expected_revision=enabled.revision,
            payload={"action": "refresh", "resource_id": "search"},
        ),
    )
    assert refreshed.success is True
    assert connection.connected is True
    reconnect = _context(_principal("control.mcp.mutate"))
    reconnected = await commands.mutate_mcp(
        reconnect,
        _command(
            reconnect.request_id,
            expected_revision=refreshed.revision,
            payload={"action": "reconnect", "resource_id": "search"},
        ),
    )
    assert reconnected.success is True
    servers = await queries.list_mcp_servers(
        _context(_principal("control.mcp.read")),
        PageRequest(limit=20),
    )
    assert servers.items[0].tool_count >= 1
    health = await queries.read_health(_context(_principal("control.health.read")))
    assert health.database in {"ok", "unavailable"}
    assert not hasattr(health, "onebot_connected")
    memory_health = await queries.read_memory_health(
        _context(_principal("control.memory.metadata.read"))
    )
    assert memory_health.index in {"ok", "degraded", "unavailable"}
    assert memory_health.embedding in {"ok", "degraded", "unavailable"}
    assert memory_health.consistency in {"ok", "degraded", "unavailable"}
    await manager.close()


async def _planable_facts(database: Database, embeddings: _PlanningEmbeddings):
    await UserProfileRepository(database).observe(user_id="1001", nickname="成员")
    facts = MemoryFactService(MemoryFactRepository(database))
    ledger = EventLedgerRepository(database)
    created = []
    for index, content in enumerate(("喜欢浓咖啡", "偏爱浓郁咖啡")):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"dream-plan-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=content,
            group_id="3001",
        )
        created.append(
            await facts.remember(
                MemoryFactCreate(
                    scope_type=MemoryScopeType.PERSON_GROUP,
                    subject_user_id="1001",
                    group_id="3001",
                    kind=MemoryKind.FACT,
                    memory_key=f"drink:{index}",
                    category="profile",
                    content=content,
                    source_type=MemorySourceType.AUTOMATIC,
                    authority=MemoryAuthority.SELF_REPORT,
                ),
                evidence=MemoryEvidenceCreate(
                    event_id=event.id,
                    source_speaker_user_id="1001",
                    relation=MemoryEvidenceRelation.SELF_STATEMENT,
                    confidence=1.0,
                    authority=MemoryAuthority.SELF_REPORT,
                    excerpt=content,
                ),
            )
        )
    blob = Float32VectorCodec().encode(_unit_vector())
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        for fact in created:
            session.add(
                MemoryEmbeddingModel(
                    fact_id=fact.id,
                    profile_id=embeddings.profile_id,
                    content_hash=embeddings.documents.content_hash(fact),
                    vector_blob=blob,
                    created_at=now,
                    updated_at=now,
                )
            )
    return tuple(created)


def test_state_revision_is_lossless_across_microseconds() -> None:
    first = datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    second = datetime(2026, 1, 1, 0, 0, 0, 2, tzinfo=UTC)
    assert state_revision(second) == state_revision(first) + 1
    assert next_updated_at(first, now=first) == first + timedelta(microseconds=1)


@pytest.mark.asyncio
async def test_rebuild_operation_ref_replays_and_rejects_missing_or_tampered_pair(
    database: Database,
) -> None:
    await UserProfileRepository(database).observe(user_id="1001", nickname="成员")
    commands = _commands(database)
    writer = _principal("control.memory.rebuild")
    context = _context(writer)
    payload = {"action": "start", "resource_id": "index"}
    first = await commands.rebuild_memory(
        context,
        _command(context.request_id, expected_revision=0, payload=payload),
    )
    assert first.operation is not None
    assert first.operation.operation_id.startswith("rebuild:")
    assert first.operation.operation_id.partition(":")[0] == "rebuild"
    replayed = await commands.rebuild_memory(
        context,
        _command(context.request_id, expected_revision=0, payload=payload),
    )
    assert replayed.operation is not None
    assert replayed.operation.operation_id == first.operation.operation_id
    assert replayed.operation.operation_id.partition(":")[0] == "rebuild"
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == context.request_id.text
            )
        )
        assert receipt is not None
        assert receipt.operation_kind == "rebuild"
        assert receipt.operation_ref == first.operation.operation_id
        receipt.operation_kind = "dream"
    with pytest.raises(ControlCommandError) as kind_mismatch:
        await commands.rebuild_memory(
            context,
            _command(context.request_id, expected_revision=0, payload=payload),
        )
    assert kind_mismatch.value.problem.code is ProblemCode.STATE_MISMATCH
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        assert receipt is not None
        receipt.operation_kind = "dream"
        receipt.operation_ref = f"dream:{first.resource_id}"
    with pytest.raises(ControlCommandError) as swapped:
        await commands.rebuild_memory(
            context,
            _command(context.request_id, expected_revision=0, payload=payload),
        )
    assert swapped.value.problem.code is ProblemCode.STATE_MISMATCH
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(ControlCommandReceiptModel))
        assert receipt is not None
        receipt.operation_kind = "rebuild"
        receipt.operation_ref = first.operation.operation_id
        run = await session.scalar(
            select(MemoryRebuildRunModel).where(
                MemoryRebuildRunModel.public_id == first.resource_id
            )
        )
        assert run is not None
        await session.delete(run)
    with pytest.raises(ControlCommandError) as missing:
        await commands.rebuild_memory(
            context,
            _command(context.request_id, expected_revision=0, payload=payload),
        )
    assert missing.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_failed_management_receipt_cannot_carry_operation_pair(
    database: Database,
) -> None:
    commands = _commands(database)
    context = _context(_principal("control.config.mutate"))
    payload = {
        "key": _CONFIG_KEY,
        "scope_type": "global",
        "scope_id": "",
        "value": False,
    }
    with pytest.raises(ControlCommandError) as conflict:
        await commands.set_config(
            context,
            _command(context.request_id, expected_revision=7, payload=payload),
        )
    assert conflict.value.problem.code is ProblemCode.VERSION_CONFLICT
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == context.request_id.text
            )
        )
        assert receipt is not None
        receipt.operation_kind = "rebuild"
        receipt.operation_ref = "rebuild:forged"
    with pytest.raises(ControlCommandError) as rejected:
        await commands.set_config(
            context,
            _command(context.request_id, expected_revision=7, payload=payload),
        )
    assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_dream_plan_persists_real_statistics_clusters_and_replays_operation(
    database: Database,
) -> None:
    embeddings = await _planning_embeddings(database)
    facts = await _planable_facts(database, embeddings)
    settings = _settings(
        database,
        memory_embedding_enabled=True,
        memory_embedding_base_url="http://127.0.0.1",
        memory_embedding_api_key="test-embedding",
    )
    expected = await plan_full_core(
        settings=settings,
        repository=DreamRepository(database),
        embeddings=embeddings,
        actor_user_id="planner",
    )
    loaded = await DreamRepository(database).load_candidates(
        profile_id=embeddings.profile_id,
        dimensions=embeddings.dimensions,
        documents=embeddings.documents,
    )
    planner = object.__new__(DreamService)
    planner._settings = settings
    planner._codec = Float32VectorCodec()
    clusters, isolated = await planner._clusters(loaded, incremental=False)
    statistics = DreamService._statistics(loaded, clusters=clusters, isolated=isolated)
    assert statistics.ready_facts == 2
    assert statistics.candidate_clusters == 1
    assert statistics.isolated_facts == 0
    assert expected.statistics == statistics
    assert expected.snapshot_max_fact_id == max(fact.id for fact in facts)
    commands = _commands(database, embeddings=embeddings)
    writer = _principal("control.memory.dream")
    context = _context(writer)
    payload = {"action": "plan", "resource_id": "index"}
    planned = await commands.dream_memory(
        context,
        _command(context.request_id, expected_revision=0, payload=payload),
    )
    assert planned.operation is not None
    assert planned.operation.operation_id.startswith("dream:")
    run = await DreamRepository(database).get_run(planned.resource_id)
    assert run is not None
    assert run.statistics == statistics
    assert run.snapshot_max_fact_id == expected.snapshot_max_fact_id
    page = await DreamRepository(database).run_page(planned.resource_id)
    assert [tuple(item.fact_ids) for item in page.clusters] == [
        tuple(item.fact.id for item in cluster) for cluster in clusters
    ]
    replayed = await commands.dream_memory(
        context,
        _command(context.request_id, expected_revision=0, payload=payload),
    )
    assert replayed.operation is not None
    assert replayed.operation.operation_id == planned.operation.operation_id
    async with database.sessions() as session, session.begin():
        row = await session.scalar(
            select(MemoryDreamRunModel).where(MemoryDreamRunModel.public_id == planned.resource_id)
        )
        assert row is not None
        await session.delete(row)
    with pytest.raises(ControlCommandError) as missing:
        await commands.dream_memory(
            context,
            _command(context.request_id, expected_revision=0, payload=payload),
        )
    assert missing.value.problem.code is ProblemCode.STATE_MISMATCH


@pytest.mark.asyncio
async def test_same_millisecond_cas_rejects_stale_revision(database: Database) -> None:
    await UserProfileRepository(database).observe(user_id="1001", nickname="成员")
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            memory_key="cas-note",
            category="fact",
            content="同毫秒修订",
            importance=1,
            source_type=MemorySourceType.EXPLICIT,
        ),
        limit=100,
    )
    loaded = await facts.get_fact(fact.id)
    assert loaded is not None
    commands = _commands(database)
    writer = _principal("control.memory.mutate")
    frozen = datetime(2026, 12, 1, 8, 0, 0, tzinfo=UTC)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen

    with patch("qq_ai_bot.memory.repository.datetime", _FrozenDateTime):
        first_ctx = _context(writer)
        first = await commands.mutate_memory(
            first_ctx,
            _command(
                first_ctx.request_id,
                expected_revision=state_revision(loaded.updated_at),
                payload={"action": "confirm", "resource_id": str(fact.id)},
            ),
        )
        second_ctx = _context(writer)
        second = await commands.mutate_memory(
            second_ctx,
            _command(
                second_ctx.request_id,
                expected_revision=first.revision,
                payload={"action": "quarantine", "resource_id": str(fact.id)},
            ),
        )
    assert second.revision != first.revision
    stale = _context(writer)
    with pytest.raises(ControlCommandError) as conflict:
        await commands.mutate_memory(
            stale,
            _command(
                stale.request_id,
                expected_revision=first.revision,
                payload={"action": "confirm", "resource_id": str(fact.id)},
            ),
        )
    assert conflict.value.problem.code is ProblemCode.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_management_semantic_tamper_matrix_rejects_impossible_status(
    database: Database,
    tmp_path: Path,
) -> None:
    embeddings = await _planning_embeddings(database)
    commands = _commands(database, embeddings=embeddings)
    writer = _principal(*_WRITE_CAPS)

    async def _replay_status(method, context, command, status: str) -> None:
        await _rewrite_success_status(database, context.request_id.text, status)
        with pytest.raises(ControlCommandError) as rejected:
            await method(context, command)
        assert rejected.value.problem.code is ProblemCode.STATE_MISMATCH

    set_ctx = _context(writer)
    set_payload = {
        "key": _CONFIG_KEY,
        "scope_type": "global",
        "scope_id": "",
        "value": False,
    }
    applied = await commands.set_config(
        set_ctx,
        _command(set_ctx.request_id, expected_revision=0, payload=set_payload),
    )
    await _replay_status(
        commands.set_config,
        set_ctx,
        _command(set_ctx.request_id, expected_revision=0, payload=set_payload),
        "removed",
    )

    unset_ctx = _context(writer)
    unset_payload = {"key": _CONFIG_KEY, "scope_type": "global", "scope_id": ""}
    current = await commands.unset_config(
        unset_ctx,
        _command(unset_ctx.request_id, expected_revision=applied.revision, payload=unset_payload),
    )
    await _replay_status(
        commands.unset_config,
        unset_ctx,
        _command(unset_ctx.request_id, expected_revision=1, payload=unset_payload),
        "applied",
    )

    restore_ctx = _context(writer)
    restored = await commands.set_config(
        restore_ctx,
        _command(
            restore_ctx.request_id,
            expected_revision=0,
            payload=set_payload,
        ),
    )
    change_id = await _runtime_change_id(database)
    rollback_ctx = _context(writer)
    rollback_payload = {"change_id": change_id}
    await commands.rollback_config(
        rollback_ctx,
        _command(
            rollback_ctx.request_id,
            expected_revision=restored.revision,
            payload=rollback_payload,
        ),
    )
    await _replay_status(
        commands.rollback_config,
        rollback_ctx,
        _command(
            rollback_ctx.request_id,
            expected_revision=restored.revision,
            payload=rollback_payload,
        ),
        "applied",
    )

    await UserProfileRepository(database).observe(user_id="1001", nickname="成员")
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            memory_key="tamper-note",
            category="fact",
            content="语义篡改",
            importance=1,
            source_type=MemorySourceType.EXPLICIT,
        ),
        limit=100,
    )
    loaded = await facts.get_fact(fact.id)
    assert loaded is not None
    memory_ctx = _context(writer)
    memory_payload = {"action": "confirm", "resource_id": str(fact.id)}
    await commands.mutate_memory(
        memory_ctx,
        _command(
            memory_ctx.request_id,
            expected_revision=state_revision(loaded.updated_at),
            payload=memory_payload,
        ),
    )
    await _replay_status(
        commands.mutate_memory,
        memory_ctx,
        _command(
            memory_ctx.request_id,
            expected_revision=state_revision(loaded.updated_at),
            payload=memory_payload,
        ),
        "quarantine",
    )

    rebuild_ctx = _context(writer)
    rebuild_payload = {"action": "start", "resource_id": "index"}
    await commands.rebuild_memory(
        rebuild_ctx,
        _command(rebuild_ctx.request_id, expected_revision=0, payload=rebuild_payload),
    )
    await _replay_status(
        commands.rebuild_memory,
        rebuild_ctx,
        _command(rebuild_ctx.request_id, expected_revision=0, payload=rebuild_payload),
        "planned",
    )

    dream_ctx = _context(writer)
    dream_payload = {"action": "plan", "resource_id": "index"}
    dreamed = await commands.dream_memory(
        dream_ctx,
        _command(dream_ctx.request_id, expected_revision=0, payload=dream_payload),
    )
    await _replay_status(
        commands.dream_memory,
        dream_ctx,
        _command(dream_ctx.request_id, expected_revision=0, payload=dream_payload),
        "running",
    )

    expired = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            memory_key="expired-tamper",
            category="fact",
            content="过期",
            importance=1,
            source_type=MemorySourceType.EXPLICIT,
            valid_until=datetime.now(UTC) - timedelta(days=1),
        ),
        limit=100,
    )
    assert expired.id
    maintain_ctx = _context(writer)
    maintain_payload = {"action": "run", "resource_id": "index"}
    await commands.maintain_memory(
        maintain_ctx,
        _command(maintain_ctx.request_id, expected_revision=0, payload=maintain_payload),
    )
    await _replay_status(
        commands.maintain_memory,
        maintain_ctx,
        _command(maintain_ctx.request_id, expected_revision=0, payload=maintain_payload),
        "running",
    )

    script = {
        "version": 1,
        "name": "篡改提醒",
        "timezone": "Asia/Shanghai",
        "schedule": {"type": "after", "seconds": 1},
        "context": {"scene": "none"},
        "steps": [
            {
                "id": "send",
                "call": "onebot.send_private_message",
                "arguments": {"user_id": "$creator_user_id", "text": "测试"},
            }
        ],
        "limits": {
            "max_steps": 1,
            "max_llm_calls": 0,
            "max_tool_calls": 1,
            "max_messages": 1,
            "timeout_seconds": 30,
        },
    }
    create_ctx = _context(writer)
    create_payload = {"action": "create", "resource_id": "yuki", "spec": script}
    created = await commands.mutate_automation(
        create_ctx,
        _command(create_ctx.request_id, expected_revision=0, payload=create_payload),
    )
    await _replay_status(
        commands.mutate_automation,
        create_ctx,
        _command(create_ctx.request_id, expected_revision=0, payload=create_payload),
        "paused",
    )
    pause_ctx = _context(writer)
    pause_payload = {"action": "pause", "resource_id": created.resource_id}
    paused = await commands.mutate_automation(
        pause_ctx,
        _command(pause_ctx.request_id, expected_revision=created.revision, payload=pause_payload),
    )
    await _replay_status(
        commands.mutate_automation,
        pause_ctx,
        _command(pause_ctx.request_id, expected_revision=created.revision, payload=pause_payload),
        "active",
    )

    plugins = PluginInstallationRepository(database)
    record = await plugins.upsert_discovered(
        plugin_id="tamper-plugin",
        name="Tamper",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="b" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish",),
    )
    approve_ctx = _context(writer)
    approve_payload = {"action": "approve", "resource_id": "tamper-plugin"}
    approved = await commands.mutate_plugin(
        approve_ctx,
        _command(
            approve_ctx.request_id,
            expected_revision=state_revision(record.updated_at),
            payload=approve_payload,
        ),
    )
    await _replay_status(
        commands.mutate_plugin,
        approve_ctx,
        _command(
            approve_ctx.request_id,
            expected_revision=state_revision(record.updated_at),
            payload=approve_payload,
        ),
        "disabled",
    )
    enable_ctx = _context(writer)
    enable_payload = {"action": "enable", "resource_id": "tamper-plugin"}
    enabled = await commands.mutate_plugin(
        enable_ctx,
        _command(
            enable_ctx.request_id,
            expected_revision=approved.revision,
            payload=enable_payload,
        ),
    )
    await _replay_status(
        commands.mutate_plugin,
        enable_ctx,
        _command(
            enable_ctx.request_id,
            expected_revision=approved.revision,
            payload=enable_payload,
        ),
        "disabled",
    )
    doctor_ctx = _context(writer)
    doctor_payload = {"action": "doctor", "resource_id": "tamper-plugin"}
    await commands.mutate_plugin(
        doctor_ctx,
        _command(doctor_ctx.request_id, expected_revision=enabled.revision, payload=doctor_payload),
    )
    await _replay_status(
        commands.mutate_plugin,
        doctor_ctx,
        _command(doctor_ctx.request_id, expected_revision=enabled.revision, payload=doctor_payload),
        "approved",
    )

    now = datetime.now(UTC)
    asset_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            EmojiAssetModel(
                id=asset_id,
                sha256="cd" * 32,
                relative_path=f"emoji/{asset_id}.png",
                image_format="png",
                mime_type="image/png",
                byte_size=12,
                width=16,
                height=16,
                frame_count=1,
                animated=False,
                status="candidate",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SpeechVoiceProfileModel(
                profile_id="tamper-voice",
                display_name="Tamper",
                provider="genie",
                engine_model_version="v2",
                language="zh",
                supported_languages_json='["zh"]',
                model_relative_path="voices/tamper/model",
                model_checksum="e" * 64,
                default_style="neutral",
                enabled=True,
                is_default=False,
                source="test",
                source_note="",
                license_note="",
                manifest_hash="f" * 64,
                created_at=now,
                updated_at=now,
            )
        )
    async with database.sessions() as session:
        emoji_row = await session.get(EmojiAssetModel, asset_id)
        speech_row = await session.get(SpeechVoiceProfileModel, "tamper-voice")
    assert emoji_row is not None and speech_row is not None
    pin_ctx = _context(writer)
    pin_payload = {"action": "pin", "resource_id": asset_id}
    await commands.mutate_emoji(
        pin_ctx,
        _command(
            pin_ctx.request_id,
            expected_revision=state_revision(emoji_row.updated_at),
            payload=pin_payload,
        ),
    )
    await _replay_status(
        commands.mutate_emoji,
        pin_ctx,
        _command(
            pin_ctx.request_id,
            expected_revision=state_revision(emoji_row.updated_at),
            payload=pin_payload,
        ),
        "banned",
    )
    speech_ctx = _context(writer)
    speech_payload = {"action": "disable", "resource_id": "tamper-voice"}
    await commands.mutate_speech(
        speech_ctx,
        _command(
            speech_ctx.request_id,
            expected_revision=state_revision(speech_row.updated_at),
            payload=speech_payload,
        ),
    )
    await _replay_status(
        commands.mutate_speech,
        speech_ctx,
        _command(
            speech_ctx.request_id,
            expected_revision=state_revision(speech_row.updated_at),
            payload=speech_payload,
        ),
        "enabled",
    )

    dream_run = await DreamRepository(database).get_run(dreamed.resource_id)
    assert dream_run is not None
    cancel_ctx = _context(writer)
    cancel_payload = {"action": "cancel", "resource_id": f"dream:{dreamed.resource_id}"}
    await commands.cancel_operation(
        cancel_ctx,
        _command(
            cancel_ctx.request_id,
            expected_revision=state_revision(dream_run.updated_at),
            payload=cancel_payload,
        ),
    )
    await _replay_status(
        commands.cancel_operation,
        cancel_ctx,
        _command(
            cancel_ctx.request_id,
            expected_revision=state_revision(dream_run.updated_at),
            payload=cancel_payload,
        ),
        "running",
    )

    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"search": {"command": "python", "lifecycle": "lazy"}}}),
        encoding="utf-8",
    )
    tool = SimpleNamespace(
        name="lookup",
        description="lookup",
        inputSchema={"type": "object", "properties": {}},
        outputSchema=None,
        annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
    )
    connection = FakeMCPConnection(tools=(tool,))

    class _Factory:
        def __call__(self, config: object, **_kwargs: object) -> FakeMCPConnection:
            return connection

    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=4,
        repository=MCPRepository(database),
        connection_factory=_Factory(),
    )
    await manager.start()
    mcp_commands = _commands(database, mcp=manager, embeddings=embeddings)
    async with database.sessions() as session:
        state = await session.get(MCPServerStateModel, "search")
    assert state is not None
    mcp_ctx = _context(writer)
    mcp_payload = {"action": "disable", "resource_id": "search"}
    await mcp_commands.mutate_mcp(
        mcp_ctx,
        _command(
            mcp_ctx.request_id,
            expected_revision=state_revision(state.updated_at),
            payload=mcp_payload,
        ),
    )
    await _replay_status(
        mcp_commands.mutate_mcp,
        mcp_ctx,
        _command(
            mcp_ctx.request_id,
            expected_revision=state_revision(state.updated_at),
            payload=mcp_payload,
        ),
        "enabled",
    )
    await manager.close()
    assert paused.success is True
    assert current.success is True
