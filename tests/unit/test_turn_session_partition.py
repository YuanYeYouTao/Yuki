"""TurnMemorySession resolves conversation_key only through MemoryPartitionLookup."""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_memory_partition import _flip_v2, _seed_binding

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.attribution import MemoryExposure, MemoryExposureSource
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_legacy_memory_partition,
)
from qq_ai_bot.memory.receipt import MemoryRecallTurn
from qq_ai_bot.memory.runtime.errors import MemorySessionClosedError
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand
from qq_ai_bot.memory.runtime.state import MemorySessionState
from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession, empty_retrieval
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.contracts import DeliverySummary
from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.chat import ChatService

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
_RECORD_RECALL_KEYS = frozenset(
    {
        "conversation_key",
        "trigger_message_id",
        "origin",
        "intent",
        "result",
        "injected_fact_ids",
        "runtime",
    }
)


class _FixedLookup:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls: list[tuple[str | None, str | None]] = []

    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str:
        self.calls.append((group_id, private_peer_user_id))
        return self.key


class _FormatLookup:
    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str:
        return format_legacy_memory_partition(
            group_id=group_id,
            private_peer_user_id=private_peer_user_id,
        )


class _FailingLookup:
    def __init__(self, reason: str = "missing_owner") -> None:
        self.reason = reason

    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str:
        del group_id, private_peer_user_id
        raise MemoryPartitionResolutionError(self.reason)


class _MinimalPartitionLookup:
    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str:
        del group_id, private_peer_user_id
        return "private:1001"


class _NonCallableResolve:
    resolve_from_scope = "not-callable"


class _RecordingContext:
    def __init__(self) -> None:
        self.record_recall_calls = 0
        self.conversation_keys: list[str] = []
        self.record_recall_kwargs: list[dict[str, object]] = []

    async def retrieve_for_turn(self, **_kwargs: object) -> object:
        return empty_retrieval()

    async def mark_injected(self, _result: object, fact_ids: tuple[int, ...]) -> int:
        return len(fact_ids)

    async def record_recall(self, **kwargs: object) -> MemoryRecallTurn:
        self.record_recall_calls += 1
        key = kwargs.get("conversation_key")
        injected = kwargs.get("injected_fact_ids")
        assert isinstance(key, str)
        assert isinstance(injected, tuple)
        self.conversation_keys.append(key)
        self.record_recall_kwargs.append(dict(kwargs))
        return MemoryRecallTurn(turn_id="receipt-1", injected_fact_ids=injected)


def _inbound(*, group_id: str | None = None, user_id: str = "1001") -> InboundMessage:
    return InboundMessage(
        message_id="m-partition",
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id),
        text="回忆一下",
        bot_user_id="bot-9",
        group_id=group_id,
    )


def _authority() -> TurnAuthority:
    return TurnAuthority(
        actor_user_id="1001",
        bot_user_id="bot-9",
        origin=TurnOrigin.USER_MESSAGE,
        permission_ceiling=frozenset(),
        delegated_authority=None,
        authority_revision=1,
    )


def _runtime() -> Any:
    return SimpleNamespace(
        memory=SimpleNamespace(retrieval_enabled=True, usage_attribution_enabled=True)
    )


def _open(
    context: _RecordingContext,
    lookup: object,
    *,
    inbound: InboundMessage | None = None,
) -> TurnMemorySession:
    message = inbound or _inbound()
    identity = (
        ConversationScope.group("bot-9", message.group_id)
        if message.group_id
        else ConversationScope.private("bot-9", message.sender.user_id)
    )
    return TurnMemorySession.open(
        inbound=message,
        identity=identity,
        runtime=_runtime(),
        memory_context=context,  # type: ignore[arg-type]
        partition_lookup=lookup,  # type: ignore[arg-type]
        origin=TurnOrigin.USER_MESSAGE,
        user_question=message.text,
        authority=_authority(),
    )


def _exposure() -> MemoryExposure:
    return MemoryExposure(
        memory_ref="M11",
        fact_id=11,
        kind="fact",
        category="pref",
        content="喜欢美式",
        occurred_at=None,
        target_role="current_person",
        source=MemoryExposureSource.AUTOMATIC,
    )


async def _stage_and_confirm(session: TurnMemorySession) -> object:
    await session.prefetch()
    session.stage_prompt_selection((11,), (_exposure(),))
    return await session.confirm_prompt_exposure()


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _class_method(tree: ast.AST, class_name: str, method_name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                    and item.name == method_name
                ):
                    return item
    raise AssertionError(f"{class_name}.{method_name} missing")


def test_open_and_init_require_partition_lookup() -> None:
    for target in (TurnMemorySession.open, TurnMemorySession.__init__):
        parameter = inspect.signature(target).parameters["partition_lookup"]
        assert parameter.default is inspect.Parameter.empty


def test_empty_retrieval_query_hash_unchanged() -> None:
    assert empty_retrieval().query_hash == hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_lookup_value_is_passed_as_conversation_key() -> None:
    context = _RecordingContext()
    lookup = _FixedLookup("person:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    session = _open(context, lookup)
    handle = await _stage_and_confirm(session)
    assert handle is not None
    assert lookup.calls == [(None, "1001")]
    assert context.conversation_keys == [lookup.key]
    assert set(context.record_recall_kwargs[0]) == _RECORD_RECALL_KEYS


@pytest.mark.asyncio
async def test_format_lookup_v1_private_key() -> None:
    context = _RecordingContext()
    session = _open(context, _FormatLookup())
    await _stage_and_confirm(session)
    assert context.conversation_keys == ["private:1001"]


@pytest.mark.asyncio
async def test_format_lookup_v1_group_key() -> None:
    context = _RecordingContext()
    lookup = _FormatLookup()
    session = _open(context, lookup, inbound=_inbound(group_id="2001"))
    await _stage_and_confirm(session)
    assert context.conversation_keys == ["group:2001"]


@pytest.mark.asyncio
async def test_database_v1_key(database: Database) -> None:
    context = _RecordingContext()
    session = _open(context, DatabaseMemoryPartitionLookup(database))
    await _stage_and_confirm(session)
    assert context.conversation_keys == ["private:1001"]


@pytest.mark.asyncio
async def test_database_v2_binding_key(database: Database) -> None:
    person_id = str(uuid4())
    await _seed_binding(database, person_id=person_id, external_id="1001")
    await _flip_v2(database)
    context = _RecordingContext()
    session = _open(context, DatabaseMemoryPartitionLookup(database))
    await _stage_and_confirm(session)
    assert context.conversation_keys == [f"person:{person_id}"]


@pytest.mark.asyncio
async def test_v2_missing_owner_fails_before_record(database: Database) -> None:
    await _flip_v2(database)
    context = _RecordingContext()
    session = _open(context, DatabaseMemoryPartitionLookup(database))
    await session.prefetch()
    session.stage_prompt_selection((11,), (_exposure(),))
    with pytest.raises(MemoryPartitionResolutionError) as exc:
        await session.confirm_prompt_exposure()
    assert exc.value.reason == "missing_owner"
    assert context.record_recall_calls == 0


@pytest.mark.asyncio
async def test_failing_lookup_does_not_record() -> None:
    context = _RecordingContext()
    session = _open(context, _FailingLookup("missing_owner"))
    await session.prefetch()
    session.stage_prompt_selection((11,), (_exposure(),))
    with pytest.raises(MemoryPartitionResolutionError) as exc:
        await session.confirm_prompt_exposure()
    assert exc.value.reason == "missing_owner"
    assert context.record_recall_calls == 0


@pytest.mark.asyncio
async def test_confirm_twice_records_once() -> None:
    context = _RecordingContext()
    session = _open(context, _FormatLookup())
    first = await _stage_and_confirm(session)
    second = await session.confirm_prompt_exposure()
    assert first is not None
    assert second is None
    assert context.record_recall_calls == 1
    assert context.conversation_keys == ["private:1001"]


@pytest.mark.asyncio
async def test_cancelled_delivery_skips_attribution() -> None:
    jobs: list[object] = []
    session = _open(_RecordingContext(), _FormatLookup())
    session._attribution = SimpleNamespace(enqueue=jobs.append)  # type: ignore[assignment]
    await _stage_and_confirm(session)
    await session.on_delivery_confirmed(
        DeliverySummary(
            final_agent_run_id="m-partition",
            status=DeliveryStatus.CANCELLED,
            delivered_text="已送达但被取消",
        )
    )
    assert jobs == []


def _required_chat_init_kwargs() -> dict[str, object]:
    return {
        name: object()
        for name, item in inspect.signature(ChatService.__init__).parameters.items()
        if name != "self"
        and name != "memory_partition_lookup"
        and item.default is inspect.Parameter.empty
    }


def _constructible_chat_init_kwargs() -> dict[str, object]:
    kwargs = _required_chat_init_kwargs()
    kwargs["settings"] = SimpleNamespace(
        llm_model="fake",
        web=SimpleNamespace(
            tavily_domains=frozenset(),
            web_allow_provider_override=True,
            web_fallback_on_access_denied=True,
            web_fallback_on_target_miss=True,
        ),
    )
    kwargs["model_executor"] = SimpleNamespace(execute=lambda *_args, **_kwargs: None)
    kwargs["memory_context"] = object()
    kwargs["conversation_scopes"] = object()
    kwargs["context_assembler"] = object()
    kwargs["prompt_composer"] = object()
    kwargs["turn_coordinator"] = object()
    kwargs["reply_sequence"] = object()
    return kwargs


def test_chat_service_lookup_omission_is_type_error() -> None:
    parameter = inspect.signature(ChatService.__init__).parameters["memory_partition_lookup"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="memory_partition_lookup"):
        ChatService(**_required_chat_init_kwargs())


def test_chat_service_lookup_none_is_type_error() -> None:
    with pytest.raises(TypeError, match="memory_partition_lookup"):
        ChatService(**_required_chat_init_kwargs(), memory_partition_lookup=None)


def test_chat_service_lookup_object_is_type_error() -> None:
    with pytest.raises(TypeError, match="memory_partition_lookup"):
        ChatService(**_required_chat_init_kwargs(), memory_partition_lookup=object())


def test_chat_service_lookup_non_callable_resolve_is_type_error() -> None:
    with pytest.raises(TypeError, match="memory_partition_lookup"):
        ChatService(
            **_required_chat_init_kwargs(),
            memory_partition_lookup=_NonCallableResolve(),
        )


def test_chat_service_lookup_minimal_fake_constructs() -> None:
    lookup = _MinimalPartitionLookup()
    chat = ChatService(
        **_constructible_chat_init_kwargs(),
        memory_partition_lookup=lookup,
    )
    assert chat._memory_partition_lookup is lookup


def test_open_memory_session_returns_none_without_context() -> None:
    chat = object.__new__(ChatService)
    chat._memory_context = None
    chat._memory_partition_lookup = None
    assert (
        chat._open_memory_session(
            _inbound(),
            ConversationScope.private("bot-9", "1001"),
            "回忆一下",
            _runtime(),
            autonomous=False,
            visual_input_present=False,
            structured_command=MemoryStructuredCommand.NONE,
        )
        is None
    )


def test_turn_session_has_no_reach_through_or_legacy_fallback() -> None:
    path = SRC_ROOT / "memory" / "runtime" / "turn_session.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = (
        "sqlalchemy",
        "qq_ai_bot.identity",
        "qq_ai_bot.persistence.database",
        "qq_ai_bot.memory.partition",
    )
    for node in ast.walk(tree):
        module = ""
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
        elif isinstance(node, ast.Import):
            module = ".".join(alias.name for alias in node.names)
        assert not any(module == item or module.startswith(f"{item}.") for item in forbidden)
    assert "_facts" not in source
    assert "getattr" not in source
    assert "identity_runtime_is_complete_v2" not in source
    method = _class_method(tree, "TurnMemorySession", "_memory_partition_key")
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
    assert {_attr_name(call.func) for call in calls} == {"resolve_from_scope"}
    confirm = ast.unparse(_class_method(tree, "TurnMemorySession", "confirm_prompt_exposure"))
    assert confirm.index("require_open") < confirm.index("_memory_partition_key")
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "partition_key"
        for node in ast.walk(method)
    )


def test_conversation_module_injects_database_partition_lookup() -> None:
    path = SRC_ROOT / "application" / "modules" / "conversation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    injected: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _attr_name(node.func) == "ChatService":
            for keyword in node.keywords:
                if keyword.arg == "memory_partition_lookup":
                    injected.append(keyword.value)
    assert len(injected) == 1
    value = injected[0]
    assert isinstance(value, ast.Call)
    assert _attr_name(value.func) == "DatabaseMemoryPartitionLookup"
    assert len(value.args) == 1
    argument = value.args[0]
    assert isinstance(argument, ast.Attribute)
    assert argument.attr == "database"
    assert isinstance(argument.value, ast.Name)
    assert argument.value.id == "persistence"


def test_chat_open_session_uses_injected_lookup_without_runtime_guard() -> None:
    path = SRC_ROOT / "services" / "chat.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    init = _class_method(tree, "ChatService", "__init__")
    lookup_arg = next(
        item
        for item in init.args.args + init.args.kwonlyargs
        if item.arg == "memory_partition_lookup"
    )
    assert lookup_arg.arg == "memory_partition_lookup"
    assert "TypeError" in ast.dump(init)
    assert "resolve_from_scope" in ast.dump(init)
    method = _class_method(tree, "ChatService", "_open_memory_session")
    dumped = ast.dump(method)
    assert "memory_partition_lookup" in dumped
    assert "RuntimeError" not in dumped
    assert "TurnMemorySession" in dumped
    assert "partition_lookup" in dumped
    assert "getattr" not in dumped
    assert "_facts" not in dumped


def _is_chat_service_construct(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "ChatService"
    return isinstance(func, ast.Attribute) and func.attr == "ChatService"


def _raises_typeerror(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or _attr_name(node.func) != "raises" or not node.args:
        return False
    return _attr_name(node.args[0]) == "TypeError"


def _walk_file_chat_service_constructs(path: Path) -> list[tuple[Path, ast.Call, bool]]:
    found: list[tuple[Path, ast.Call, bool]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def visit(node: ast.AST, in_typeerror: bool = False) -> None:
        if isinstance(node, ast.With) and any(
            _raises_typeerror(item.context_expr) for item in node.items
        ):
            for child in ast.iter_child_nodes(node):
                visit(child, True)
            return
        if _is_chat_service_construct(node):
            assert isinstance(node, ast.Call)
            found.append((path, node, in_typeerror))
        for child in ast.iter_child_nodes(node):
            visit(child, in_typeerror)

    visit(tree)
    return found


def _walk_chat_service_constructs(root: Path) -> list[tuple[Path, ast.Call, bool]]:
    found: list[tuple[Path, ast.Call, bool]] = []
    for path in root.rglob("*.py"):
        found.extend(_walk_file_chat_service_constructs(path))
    return found


def test_exactly_two_real_chat_service_constructors_inject_lookup() -> None:
    calls = _walk_chat_service_constructs(REPO_ROOT / "src") + _walk_chat_service_constructs(
        REPO_ROOT / "tests"
    )
    live = [(path, node) for path, node, in_typeerror in calls if not in_typeerror]
    omitted = [item for item in calls if item[2]]
    assert omitted
    injected = []
    extras = []
    for path, node in live:
        keyword = next(item for item in node.keywords if item.arg == "memory_partition_lookup")
        value = keyword.value
        if (
            isinstance(value, ast.Call)
            and _attr_name(value.func) == "DatabaseMemoryPartitionLookup"
        ):
            injected.append((path, node, value))
            continue
        extras.append((path, node))
        assert path.name == "test_turn_session_partition.py"
    assert len(injected) == 2
    assert extras
    names = {path.name for path, _node, _value in injected}
    assert names == {"conversation.py", "conftest.py"}
    for path, _node, value in injected:
        assert isinstance(value, ast.Call)
        assert len(value.args) == 1
        argument = value.args[0]
        if path.name == "conftest.py":
            assert isinstance(argument, ast.Name)
            assert argument.id == "database"
            continue
        assert isinstance(argument, ast.Attribute)
        assert argument.attr == "database"
        assert isinstance(argument.value, ast.Name)
        assert argument.value.id == "persistence"


@pytest.mark.asyncio
async def test_confirm_after_close_does_not_lookup_or_record() -> None:
    context = _RecordingContext()
    lookup = _FixedLookup("private:1001")
    session = _open(context, lookup)
    await session.prefetch()
    session.stage_prompt_selection((11,), (_exposure(),))
    await session.close()
    with pytest.raises(MemorySessionClosedError):
        await session.confirm_prompt_exposure()
    assert lookup.calls == []
    assert context.record_recall_calls == 0
    assert context.conversation_keys == []


def test_memory_session_state_require_open_is_public() -> None:
    assert callable(MemorySessionState.require_open)
    session = _open(_RecordingContext(), _FixedLookup("private:1001"))
    session._state.require_open()
    session._state.close()
    with pytest.raises(MemorySessionClosedError):
        session._state.require_open()
