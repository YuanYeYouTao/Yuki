"""C24 live observations for plugin background and autonomous group turns."""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.gateway import napcat_registry

from qq_ai_bot.admin.models import ConversationRuntimeConfig
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.participation import AdmissionFeatures
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.conversation.rollup.repository import ConversationScopeRepository
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_space_preconfig,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig as ensure_v2_presence,
)
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel, RuntimeTurnObservationModel
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.plugin_host.background_turns import (
    PluginBackgroundTurnWorker,
    _authoritative_plugin_observation_refs,
)
from qq_ai_bot.plugin_host.notification_repository import (
    BackgroundTurnJobRecord,
    PluginNotificationRepository,
)
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.runtime.observability import claim_runtime_turn_id
from qq_ai_bot.services.autonomous_groups import (
    AutonomousGroupService,
    _authoritative_autonomous_observation_refs,
    _GroupState,
)
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
BACKGROUND_PATH = SRC_ROOT / "plugin_host" / "background_turns.py"
AUTONOMOUS_PATH = SRC_ROOT / "services" / "autonomous_groups.py"
_CANONICAL_KWARGS = (
    "canonical_conversation_id",
    "canonical_person_id",
    "canonical_space_id",
)
_FORBIDDEN_REF_SOURCES = frozenset(
    {
        "target_id",
        "bot_user_id",
        "group_id",
        "hash_conversation_key",
        "ConversationScope",
    }
)

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-4466554400c4"
PLUGIN_ID = "com.example.c24-bg-obs"


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _build_turn_observation_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "build_turn_observation"
    ]


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


def _keyword_mentions_forbidden(call: ast.Call, name: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg != name:
            continue
        for child in ast.walk(keyword.value):
            if _attr_name(child) in _FORBIDDEN_REF_SOURCES:
                return True
            if isinstance(child, ast.Name) and child.id in _FORBIDDEN_REF_SOURCES:
                return True
    return False


def _assigned_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Tuple | ast.List):
        names: set[str] = set()
        for elt in node.elts:
            names.update(_assigned_names(elt))
        return names
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _reads_live_messages(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "messages":
            return True
        if isinstance(child, ast.Name) and child.id == "messages":
            return True
    return False


def _run_latest_function(tree: ast.AST) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AutonomousGroupService":
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef)
                    and item.name == "_run_latest"
                ):
                    return item
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "_run_latest":
            return node
    raise AssertionError("_run_latest missing")


class _RunLatestOrderVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.events: list[str] = []
        self._in_finally = 0
        self._in_handler = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Try(self, node: ast.Try) -> None:
        for stmt in node.body:
            self.visit(stmt)
        self._in_handler += 1
        try:
            for handler in node.handlers:
                self.visit(handler)
            for stmt in node.orelse:
                self.visit(stmt)
        finally:
            self._in_handler -= 1
        self._in_finally += 1
        try:
            for stmt in node.finalbody:
                self.visit(stmt)
        finally:
            self._in_finally -= 1

    def visit_Assign(self, node: ast.Assign) -> None:
        targets: set[str] = set()
        for target in node.targets:
            targets.update(_assigned_names(target))
        if "selected" in targets or _reads_live_messages(node):
            self.events.append("select")
        if targets & {"canonical_conversation_id", "canonical_space_id"}:
            self.events.append("refs")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        targets = _assigned_names(node.target)
        if "selected" in targets or (node.value is not None and _reads_live_messages(node)):
            self.events.append("select")
        if targets & {"canonical_conversation_id", "canonical_space_id"}:
            self.events.append("refs")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _attr_name(node.func) == "_authoritative_autonomous_observation_refs":
            self.events.append("refs")
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        if self._in_finally:
            self.events.append("finally_await")
        elif self._in_handler:
            self.events.append("handler_await")
        elif isinstance(node.value, ast.Call) and _attr_name(node.value.func) == "_admit_latest":
            self.events.append("admit")
        else:
            self.events.append("await")
        self.generic_visit(node)


def _finally_observation_calls(fn: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.finalbody:
            for child in ast.walk(stmt):
                if (
                    isinstance(child, ast.Call)
                    and _attr_name(child.func) == "build_turn_observation"
                ):
                    calls.append(child)
    return calls


def _finally_reads_live_messages(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        if any(_reads_live_messages(stmt) for stmt in node.finalbody):
            return True
    return False


def _run_latest_snapshot_violations(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    visitor = _RunLatestOrderVisitor()
    for stmt in fn.body:
        visitor.visit(stmt)
    events = visitor.events
    violations: list[str] = []
    if "select" not in events:
        violations.append("selected inbound is not captured")
    if "refs" not in events:
        violations.append("canonical refs are not captured")
    if "admit" not in events:
        violations.append("snapshot/admit is not awaited")
    blocking = [index for index, event in enumerate(events) if event in {"await", "admit"}]
    if "select" in events and blocking and events.index("select") >= blocking[0]:
        violations.append("selected inbound is captured after the first await")
    if "refs" in events and blocking and events.index("refs") >= blocking[0]:
        violations.append("canonical refs are captured after the first await")
    if "select" in events and "admit" in events:
        if "await" in events[events.index("select") : events.index("admit")]:
            violations.append("await inserted between selection and snapshot/admit")
    if "refs" in events and "admit" in events:
        if "await" in events[events.index("refs") : events.index("admit")]:
            violations.append("await inserted between ref snapshot and admit")
    if blocking and events[blocking[0]] != "admit":
        violations.append("first await is not snapshot/admit")
    if _finally_reads_live_messages(fn):
        violations.append("finally reads state.messages/messages[-1]")
    observation_calls = _finally_observation_calls(fn)
    if not observation_calls:
        violations.append("finally does not build the captured observation")
    for call in observation_calls:
        for name in ("canonical_conversation_id", "canonical_space_id"):
            value = next((keyword.value for keyword in call.keywords if keyword.arg == name), None)
            if not isinstance(value, ast.Name) or value.id != name:
                violations.append(f"finally does not reuse captured {name}")
    return violations


def test_both_writers_forward_canonical_observation_kwargs() -> None:
    for path in (BACKGROUND_PATH, AUTONOMOUS_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = _build_turn_observation_calls(tree)
        assert calls, f"{path.name} must call build_turn_observation"
        missing = [
            f"{path.name}:{call.lineno}:{name}"
            for call in calls
            for name in _CANONICAL_KWARGS
            if not _has_keyword(call, name)
        ]
        assert missing == [], f"writers missing canonical kwargs: {missing}"
        derived = [
            f"{path.name}:{call.lineno}:{name}"
            for call in calls
            for name in _CANONICAL_KWARGS
            if _keyword_mentions_forbidden(call, name)
        ]
        assert derived == [], f"writers derive canonical refs from raw keys: {derived}"


def test_plugin_observation_helper_never_uses_raw_target() -> None:
    tree = ast.parse(BACKGROUND_PATH.read_text(encoding="utf-8"))
    helper = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "_authoritative_plugin_observation_refs"
        ):
            helper = node
            break
    assert helper is not None
    raw_hits = [
        child.lineno
        for child in ast.walk(helper)
        if _attr_name(child) in {"target_id", "bot_user_id", "group_id"}
    ]
    assert raw_hits == []


def test_run_latest_ast_captures_selected_refs_before_first_await() -> None:
    tree = ast.parse(AUTONOMOUS_PATH.read_text(encoding="utf-8"), filename=str(AUTONOMOUS_PATH))
    assert _run_latest_snapshot_violations(_run_latest_function(tree)) == []

    clean = ast.parse(
        "\n".join(
            (
                "async def _run_latest(self, scope_key, revision, runtime):",
                "    state = self._states.get(scope_key)",
                "    selected = (",
                "        state.messages[-1] if state is not None and state.messages else None",
                "    )",
                "    canonical_conversation_id, canonical_space_id = (",
                "        _authoritative_autonomous_observation_refs(selected)",
                "    )",
                "    try:",
                "        await self._admit_latest(scope_key, revision, runtime)",
                "    finally:",
                "        observation = build_turn_observation(",
                "            canonical_conversation_id=canonical_conversation_id,",
                "            canonical_space_id=canonical_space_id,",
                "        )",
                "        await record_observation_safely(self._turn_observations, observation)",
            )
        )
    )
    assert _run_latest_snapshot_violations(_run_latest_function(clean)) == []

    injected_await = ast.parse(
        "\n".join(
            (
                "async def _run_latest(self, scope_key, revision, runtime):",
                "    state = self._states.get(scope_key)",
                "    selected = (",
                "        state.messages[-1] if state is not None and state.messages else None",
                "    )",
                "    canonical_conversation_id, canonical_space_id = (",
                "        _authoritative_autonomous_observation_refs(selected)",
                "    )",
                "    await asyncio.sleep(0)",
                "    try:",
                "        await self._admit_latest(scope_key, revision, runtime)",
                "    finally:",
                "        observation = build_turn_observation(",
                "            canonical_conversation_id=canonical_conversation_id,",
                "            canonical_space_id=canonical_space_id,",
                "        )",
                "        await record_observation_safely(self._turn_observations, observation)",
            )
        )
    )
    injected_hits = _run_latest_snapshot_violations(_run_latest_function(injected_await))
    assert injected_hits, "gate must fail when an await is inserted before snapshot/admit"

    reread = ast.parse(
        "\n".join(
            (
                "async def _run_latest(self, scope_key, revision, runtime):",
                "    state = self._states.get(scope_key)",
                "    selected = (",
                "        state.messages[-1] if state is not None and state.messages else None",
                "    )",
                "    canonical_conversation_id, canonical_space_id = (",
                "        _authoritative_autonomous_observation_refs(selected)",
                "    )",
                "    try:",
                "        await self._admit_latest(scope_key, revision, runtime)",
                "    finally:",
                "        latest = state.messages[-1]",
                "        observation = build_turn_observation(",
                "            canonical_conversation_id=latest.conversation_id,",
                "            canonical_space_id=latest.space_id,",
                "        )",
                "        await record_observation_safely(self._turn_observations, observation)",
            )
        )
    )
    reread_hits = _run_latest_snapshot_violations(_run_latest_function(reread))
    assert reread_hits, "gate must fail when finally rereads messages[-1]"


class _Bot:
    def __init__(self, self_id: str) -> None:
        self.self_id = self_id

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


class _TouchingChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def configure_runtime_controls(self, _runtime: object) -> None:
        return None

    async def generate_external_reply(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        assert claim_runtime_turn_id() is not None
        return SimpleNamespace(text="obs-reply", tool_calls_used=0, model_requests=1)

    async def respond(self, *_args: object, **_kwargs: object) -> None:
        assert claim_runtime_turn_id() is not None


class _FakeTurns:
    def configure_policy(self, **_kwargs: object) -> None:
        return None

    async def begin_background(self, _key: str) -> SimpleNamespace:
        return SimpleNamespace(version=1)

    @asynccontextmanager
    async def track(self, _token: object, _kind: str):
        yield


class _FakeRuntime:
    async def snapshot(self, **kwargs: object) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(
            reply=SimpleNamespace(cancel_on_new_message=False),
            conversation_policy=lambda: SimpleNamespace(interrupt_autonomous_on_new_message=False),
        )


class _V1Scopes:
    async def get(self, scope: ConversationScope) -> SimpleNamespace:
        return SimpleNamespace(id=1, runtime_scope_key=scope.key, generation=1)


class _AdmitAll:
    async def admission_features(self, **_kwargs: object) -> AdmissionFeatures:
        return AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="请帮我查一下这是什么？你觉得怎么样",
            pending_message_count=8,
            idle_seconds=90,
            recent_total_messages=8,
        )


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


async def _install(database: Database) -> None:
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id=PLUGIN_ID,
        name="C24-bg-obs",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="c" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish", "notification.agent"),
    )
    await repository.approve(PLUGIN_ID)
    await repository.set_enabled(PLUGIN_ID, enabled=True)
    await repository.set_status(PLUGIN_ID, status="running")


def _publish_request(
    target: NotificationTarget,
    *,
    event_key: str,
) -> PublishNotificationRequest:
    return PublishNotificationRequest(
        event_key=event_key,
        event_type="test",
        external_source="test",
        target=target,
        occurred_at=_NOW,
        summary="summary",
        payload={"k": "v"},
        text="hello",
        ask_agent=True,
        agent_intent="reply",
    )


async def _grant_and_publish(
    database: Database,
    *,
    target: NotificationTarget,
    event_key: str,
) -> PluginNotificationRepository:
    notifications = PluginNotificationRepository(database)
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key=event_key),
    )
    return notifications


async def _observation_rows(database: Database) -> list[RuntimeTurnObservationModel]:
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(RuntimeTurnObservationModel).order_by(RuntimeTurnObservationModel.id.asc())
            )
        )


def _plugin_worker(
    *,
    database: Database,
    notifications: PluginNotificationRepository,
    router: PresenceRouter | None = None,
    conversation_scopes: object | None = None,
) -> PluginBackgroundTurnWorker:
    return PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=EventLedgerRepository(database),
        runtime_config=_FakeRuntime(),  # type: ignore[arg-type]
        chat=_TouchingChat(),  # type: ignore[arg-type]
        turns=_FakeTurns(),  # type: ignore[arg-type]
        conversation_scopes=conversation_scopes or SimpleNamespace(),  # type: ignore[arg-type]
        turn_observations=RuntimeTurnObservationRepository(database),
        router=router,
    )


async def _bind_person_route(
    database: Database,
    *,
    person: str,
    presence: str,
    gateway_id: str,
) -> PresenceRouter:
    registry = napcat_registry(gateway_instance_id=gateway_id)
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    assert await router.cas_takeover_person(person) == "taken"
    return router


async def _bind_space_route(
    database: Database,
    *,
    space: str,
    presence: str,
    gateway_id: str,
) -> PresenceRouter:
    registry = napcat_registry(gateway_instance_id=gateway_id)
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    assert await router.cas_takeover_space(space) == "taken"
    return router


async def _add_space_conversation(
    session: object,
    *,
    conversation_id: str,
    space_id: str,
    scope_key: str,
) -> None:
    alias_id = str(uuid4())
    session.add(
        CanonicalConversationModel(
            id=conversation_id,
            kind="space",
            person_id=None,
            space_id=space_id,
            primary_alias_id=alias_id,
            primary_marker=1,
            generation=1,
            starts_after_event_id=10_000,
            last_event_id=10_000,
            last_generation_change_event_id=10_000,
            covered_through_event_id=10_000,
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


def _group_message(
    *,
    message_id: str,
    conversation_id: str | None = None,
    space_id: str | None = None,
    legacy_conversation_key: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", group_card="远野"),
        text="请帮我查一下这是什么？你觉得怎么样",
        bot_user_id="8000",
        group_id="2001",
        conversation_id=conversation_id,
        space_id=space_id,
        person_id=None,
        legacy_conversation_key=legacy_conversation_key,
    )


def _conversation_runtime() -> SimpleNamespace:
    policy = ConversationRuntimeConfig(
        autonomous_enabled=True,
        autonomous_debounce_seconds=0.02,
        autonomous_admission_threshold=0,
        autonomous_batch_limit=20,
        autonomous_presence_window_seconds=120,
        interrupt_autonomous_on_new_message=True,
    )
    return SimpleNamespace(
        conversation=policy,
        conversation_policy=lambda: policy,
        reply=SimpleNamespace(cancel_on_new_message=False),
    )


class _SnapshotRuntime:
    async def snapshot(self, **_kwargs: object) -> SimpleNamespace:
        return _conversation_runtime()


def _job(
    *,
    target_type: str,
    person_id: str | None = None,
    space_id: str | None = None,
    conversation_id: str | None = None,
    presence_id: str | None = None,
) -> BackgroundTurnJobRecord:
    return BackgroundTurnJobRecord(
        id=1,
        source_event_id=1,
        plugin_id=PLUGIN_ID,
        target_type=target_type,
        target_id="1001" if target_type == "private" else "2001",
        bot_user_id="8000",
        agent_intent="reply",
        attempts=1,
        canonical_target_person_id=person_id,
        canonical_target_space_id=space_id,
        canonical_presence_id=presence_id,
        canonical_conversation_id=conversation_id,
    )


@pytest.mark.asyncio
async def test_v2_plugin_private_observation_stamps_person_and_conversation(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="obs-private",
    )
    router = await _bind_person_route(
        database,
        person=person,
        presence=presence,
        gateway_id="gw-c24-obs-private",
    )
    worker = _plugin_worker(database=database, notifications=notifications, router=router)
    job = await notifications.claim_turn()
    assert job is not None
    assert job.canonical_target_person_id == person
    assert job.canonical_conversation_id
    await worker._execute(job)
    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].origin == "plugin_background"
    assert rows[0].scope_type == "private"
    assert rows[0].canonical_conversation_id == job.canonical_conversation_id
    assert rows[0].canonical_person_id == person
    assert rows[0].canonical_space_id is None


@pytest.mark.asyncio
async def test_v2_plugin_group_observation_stamps_space_and_conversation(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="group", target_id="2001"),
        event_key="obs-group",
    )
    router = await _bind_space_route(
        database,
        space=space,
        presence=presence,
        gateway_id="gw-c24-obs-group",
    )
    worker = _plugin_worker(database=database, notifications=notifications, router=router)
    job = await notifications.claim_turn()
    assert job is not None
    assert job.canonical_target_space_id == space
    assert job.canonical_conversation_id
    await worker._execute(job)
    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].origin == "plugin_background"
    assert rows[0].scope_type == "group"
    assert rows[0].canonical_conversation_id == job.canonical_conversation_id
    assert rows[0].canonical_space_id == space
    assert rows[0].canonical_person_id is None


@pytest.mark.asyncio
async def test_v2_plugin_no_conversation_stamps_person_only(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = PluginNotificationRepository(database)
    worker = _plugin_worker(database=database, notifications=notifications)

    async def _touch_admitted(
        job: BackgroundTurnJobRecord,
        resolved_key: list[str] | None = None,
    ) -> None:
        del job, resolved_key
        claim_runtime_turn_id()

    worker._execute_admitted = _touch_admitted  # type: ignore[method-assign]
    job = _job(target_type="private", person_id=person, presence_id=presence)
    assert _authoritative_plugin_observation_refs(job) == (None, person, None)
    await worker._execute(job)
    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].canonical_conversation_id is None
    assert rows[0].canonical_person_id == person
    assert rows[0].canonical_space_id is None


@pytest.mark.asyncio
async def test_v2_autonomous_group_stamps_space_and_conversation(database: Database) -> None:
    await _flip_v2(database)
    conversation_id = str(uuid4())
    scope_key = ConversationScope.group("8000", "2001").key
    async with database.sessions() as session, session.begin():
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
        await _add_space_conversation(
            session,
            conversation_id=conversation_id,
            space_id=space,
            scope_key=scope_key,
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="auto-1",
                scope_type="group",
                group_id="2001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="请帮我查一下这是什么？你觉得怎么样",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                ingress_presence_id=presence,
                suppression_status="keeper",
            )
        )
    coordinator = ConversationTurnCoordinator()
    chat = _TouchingChat()
    chat._conversation_scopes = ConversationScopeRepository(database)
    chat._ledger = EventLedgerRepository(database)
    chat._event_publisher = None
    runtime = _SnapshotRuntime()
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, _AdmitAll()),
        runtime_config=cast(Any, runtime),
        turn_coordinator=coordinator,
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    inbound = _group_message(
        message_id="auto-1",
        conversation_id=conversation_id,
        space_id=space,
        legacy_conversation_key=scope_key,
    )
    state = _GroupState()
    state.messages.append(inbound)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    state.revision = 1
    state.latest_token = await coordinator.notify_message(scope_key)
    service._states[scope_key] = state
    await service._run_latest(scope_key, 1, _conversation_runtime())
    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].origin == "autonomous_group"
    assert rows[0].scope_type == "group"
    assert rows[0].canonical_conversation_id == conversation_id
    assert rows[0].canonical_space_id == space
    assert rows[0].canonical_person_id is None


@pytest.mark.asyncio
async def test_v2_autonomous_observation_keeps_selected_refs_when_deque_grows(
    database: Database,
) -> None:
    await _flip_v2(database)
    conversation_a = str(uuid4())
    conversation_b = str(uuid4())
    scope_key = ConversationScope.group("8000", "2001").key
    async with database.sessions() as session, session.begin():
        space_a = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        space_b = await ensure_canonical_space_preconfig(session, "2999", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
        await _add_space_conversation(
            session,
            conversation_id=conversation_a,
            space_id=space_a,
            scope_key=scope_key,
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="auto-a",
                scope_type="group",
                group_id="2001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="请帮我查一下这是什么？你觉得怎么样",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_a,
                ingress_presence_id=presence,
                suppression_status="keeper",
            )
        )
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingChat(_TouchingChat):
        async def respond(self, inbound: InboundMessage, *_args: object, **_kwargs: object) -> None:
            assert inbound.message_id == "auto-a"
            assert claim_runtime_turn_id() is not None
            started.set()
            await release.wait()

    coordinator = ConversationTurnCoordinator()
    chat = _BlockingChat()
    chat._conversation_scopes = ConversationScopeRepository(database)
    chat._ledger = EventLedgerRepository(database)
    chat._event_publisher = None
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, _AdmitAll()),
        runtime_config=cast(Any, _SnapshotRuntime()),
        turn_coordinator=coordinator,
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    inbound_a = _group_message(
        message_id="auto-a",
        conversation_id=conversation_a,
        space_id=space_a,
        legacy_conversation_key=scope_key,
    )
    inbound_b = _group_message(
        message_id="auto-b",
        conversation_id=conversation_b,
        space_id=space_b,
        legacy_conversation_key=scope_key,
    )
    assert inbound_a.conversation_id != inbound_b.conversation_id
    assert inbound_a.space_id != inbound_b.space_id
    state = _GroupState()
    state.messages.append(inbound_a)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    state.revision = 1
    state.latest_token = await coordinator.notify_message(scope_key)
    service._states[scope_key] = state
    task = asyncio.create_task(service._run_latest(scope_key, 1, _conversation_runtime()))
    await started.wait()
    state.messages.append(inbound_b)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    assert state.messages[-1] is inbound_b
    release.set()
    await task
    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].origin == "autonomous_group"
    assert rows[0].canonical_conversation_id == conversation_a
    assert rows[0].canonical_space_id == space_a
    assert rows[0].canonical_conversation_id != conversation_b
    assert rows[0].canonical_space_id != space_b
    assert rows[0].canonical_person_id is None


@pytest.mark.asyncio
async def test_v1_plugin_and_autonomous_observations_stay_none(database: Database) -> None:
    from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository

    await _install(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await PeopleRepository(database).observe(user_id="1001", nickname="A")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="obs-v1",
    )
    worker = _plugin_worker(
        database=database,
        notifications=notifications,
        conversation_scopes=_V1Scopes(),
    )
    job = await notifications.claim_turn()
    assert job is not None
    await worker._execute(job)
    plugin_rows = await _observation_rows(database)
    assert len(plugin_rows) == 1
    assert plugin_rows[0].canonical_conversation_id is None
    assert plugin_rows[0].canonical_person_id is None
    assert plugin_rows[0].canonical_space_id is None

    async with database.sessions() as session, session.begin():
        session.add(
            ConversationScopeModel(
                scope_key=ConversationScope.group("8000", "2001").key,
                bot_user_id="8000",
                scope_type="group",
                group_id="2001",
                generation=1,
                starts_after_event_id=0,
                last_event_id=1,
                last_generation_change_event_id=0,
                uncovered_event_count=0,
                uncovered_character_count=0,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="v1-auto",
                scope_type="group",
                group_id="2001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="请帮我查一下这是什么？你觉得怎么样",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
            )
        )
    coordinator = ConversationTurnCoordinator()
    chat = _TouchingChat()
    chat._conversation_scopes = ConversationScopeRepository(database)
    chat._ledger = EventLedgerRepository(database)
    chat._event_publisher = None
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, _AdmitAll()),
        runtime_config=cast(Any, _SnapshotRuntime()),
        turn_coordinator=coordinator,
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    scope_key = ConversationScope.group("8000", "2001").key
    inbound = _group_message(message_id="v1-auto")
    assert _authoritative_autonomous_observation_refs(inbound) == (None, None)
    state = _GroupState()
    state.messages.append(inbound)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    state.revision = 1
    state.latest_token = await coordinator.notify_message(scope_key)
    service._states[scope_key] = state
    await service._run_latest(scope_key, 1, _conversation_runtime())
    rows = await _observation_rows(database)
    assert len(rows) == 2
    assert all(row.canonical_conversation_id is None for row in rows)
    assert all(row.canonical_person_id is None for row in rows)
    assert all(row.canonical_space_id is None for row in rows)


@pytest.mark.asyncio
async def test_wrong_kind_conversation_rolls_back_partial_observation(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = PluginNotificationRepository(database)
    worker = _plugin_worker(database=database, notifications=notifications)

    async def _touch_admitted(
        job: BackgroundTurnJobRecord,
        resolved_key: list[str] | None = None,
    ) -> None:
        del job, resolved_key
        claim_runtime_turn_id()

    worker._execute_admitted = _touch_admitted  # type: ignore[method-assign]
    job = _job(
        target_type="private",
        person_id=person,
        conversation_id=space,
        presence_id=presence,
    )
    await worker._execute(job)
    assert await _observation_rows(database) == []


async def _settle_shielded_tasks() -> None:
    current = asyncio.current_task()
    for _ in range(32):
        leftover = [item for item in asyncio.all_tasks() if item is not current and not item.done()]
        if not leftover:
            return
        await asyncio.wait(leftover, timeout=0.05)


def _assert_optional_cancelled_observation(
    rows: list[RuntimeTurnObservationModel],
    *,
    conversation_a: str,
    space_a: str,
    conversation_b: str,
    space_b: str,
) -> None:
    assert len(rows) <= 1
    leaked = (
        conversation_a,
        conversation_b,
        space_a,
        space_b,
        "2001",
        "2999",
        "auto-a",
        "auto-b",
    )
    for row in rows:
        assert row.origin == "autonomous_group"
        assert row.canonical_conversation_id == conversation_a
        assert row.canonical_space_id == space_a
        assert row.canonical_person_id is None
        assert row.canonical_conversation_id != conversation_b
        assert row.canonical_space_id != space_b
        assert row.handled is False
        assert row.error_category == "CancelledError"
        assert row.error_category.isidentifier()
        assert all(token not in row.error_category for token in leaked)


@pytest.mark.asyncio
async def test_v2_autonomous_cancel_keeps_selected_refs(database: Database) -> None:
    await _flip_v2(database)
    conversation_a = str(uuid4())
    conversation_b = str(uuid4())
    scope_key = ConversationScope.group("8000", "2001").key
    async with database.sessions() as session, session.begin():
        space_a = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        space_b = await ensure_canonical_space_preconfig(session, "2999", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
        await _add_space_conversation(
            session,
            conversation_id=conversation_a,
            space_id=space_a,
            scope_key=scope_key,
        )
        await _add_space_conversation(
            session,
            conversation_id=conversation_b,
            space_id=space_b,
            scope_key=ConversationScope.group("8000", "2999").key,
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="auto-a",
                scope_type="group",
                group_id="2001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="请帮我查一下这是什么？你觉得怎么样",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_a,
                ingress_presence_id=presence,
                suppression_status="keeper",
            )
        )
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingChat(_TouchingChat):
        async def respond(self, inbound: InboundMessage, *_args: object, **_kwargs: object) -> None:
            assert inbound.message_id == "auto-a"
            assert inbound.conversation_id == conversation_a
            assert inbound.space_id == space_a
            assert claim_runtime_turn_id() is not None
            started.set()
            await release.wait()

    coordinator = ConversationTurnCoordinator()
    chat = _BlockingChat()
    chat._conversation_scopes = ConversationScopeRepository(database)
    chat._ledger = EventLedgerRepository(database)
    chat._event_publisher = None
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, _AdmitAll()),
        runtime_config=cast(Any, _SnapshotRuntime()),
        turn_coordinator=coordinator,
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    inbound_a = _group_message(
        message_id="auto-a",
        conversation_id=conversation_a,
        space_id=space_a,
        legacy_conversation_key=scope_key,
    )
    inbound_b = _group_message(
        message_id="auto-b",
        conversation_id=conversation_b,
        space_id=space_b,
        legacy_conversation_key=scope_key,
    )
    assert inbound_a.conversation_id != inbound_b.conversation_id
    assert inbound_a.space_id != inbound_b.space_id
    state = _GroupState()
    state.messages.append(inbound_a)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    state.revision = 1
    state.latest_token = await coordinator.notify_message(scope_key)
    service._states[scope_key] = state
    task = asyncio.create_task(service._run_latest(scope_key, 1, _conversation_runtime()))
    await started.wait()
    state.messages.append(inbound_b)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    assert state.messages[-1] is inbound_b
    task.cancel()
    outcome = await asyncio.gather(task, return_exceptions=True)
    assert outcome and isinstance(outcome[0], asyncio.CancelledError)
    await _settle_shielded_tasks()
    _assert_optional_cancelled_observation(
        await _observation_rows(database),
        conversation_a=conversation_a,
        space_a=space_a,
        conversation_b=conversation_b,
        space_b=space_b,
    )


@pytest.mark.asyncio
async def test_v2_autonomous_empty_deque_writes_no_observation(database: Database) -> None:
    await _flip_v2(database)
    scope_key = ConversationScope.group("8000", "2001").key
    async with database.sessions() as session, session.begin():
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        await ensure_v2_presence(session, "8000")

    class _RefuseChat(_TouchingChat):
        async def generate_external_reply(self, **kwargs: object) -> SimpleNamespace:
            del kwargs
            raise AssertionError("empty deque must not infer or start a turn")

        async def respond(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("empty deque must not infer or start a turn")

    chat = _RefuseChat()
    chat._conversation_scopes = ConversationScopeRepository(database)
    chat._ledger = EventLedgerRepository(database)
    chat._event_publisher = None
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, _AdmitAll()),
        runtime_config=cast(Any, _SnapshotRuntime()),
        turn_coordinator=ConversationTurnCoordinator(),
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    state = _GroupState()
    service._states[scope_key] = state
    assert not state.messages
    assert _authoritative_autonomous_observation_refs(None) == (None, None)
    await service._run_latest(scope_key, 1, _conversation_runtime())
    assert list(state.messages) == []
    assert await _observation_rows(database) == []
