"""C24 plugin Agent web and direct MCP Conversation correlation call-chains."""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.fakes import FakeWebSearchProvider

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.c24_conversation import (
    CANONICAL_KIND_MISMATCH,
    MISSING_CANONICAL_CONVERSATION,
)
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_presence_preconfig,
    ensure_canonical_space_preconfig,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.mcp.binding import MCPPolicyRuntime, MCPToolBinding
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MCPToolCacheModel,
    ToolInvocationModel,
    WebSearchRunModel,
)
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.plugin_host.agent_backend import PluginAgentToolBackend
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
    _MCPFacade,
)
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.web.models import WebMode, WebSearchRequest, WebSearchResponse, WebSearchSource
from yuki_plugin_sdk.context import MCPFacade
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing.fake_services import FakeMCPFacade

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-4466554400c4"
_COLLISION_UUID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
_CORRELATION_NAMES = frozenset(
    {
        "canonical_conversation_id",
        "bot_user_id",
        "ingress_presence_id",
    }
)


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _add_conversation(
    session: Any,
    *,
    conversation_id: str,
    person_id: str,
    scope_key: str,
) -> None:
    alias_id = str(uuid4())
    session.add(
        CanonicalConversationModel(
            id=conversation_id,
            kind="private",
            person_id=person_id,
            space_id=None,
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


async def _seed_pair(database: Database) -> tuple[str, str, str, str, str]:
    conversation_a = str(uuid4())
    conversation_b = str(uuid4())
    async with database.sessions() as session, session.begin():
        person_a = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        person_b = await ensure_canonical_person_preconfig(session, "1002", now=_NOW)
        presence_a = await ensure_canonical_presence_preconfig(session, "8000", now=_NOW)
        presence_b = await ensure_canonical_presence_preconfig(session, "8001", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=conversation_a,
            person_id=person_a,
            scope_key="bot:8000:private:1001",
        )
        await _add_conversation(
            session,
            conversation_id=conversation_b,
            person_id=person_b,
            scope_key="bot:8001:private:1002",
        )
    return conversation_a, conversation_b, person_a, presence_a, presence_b


def _inbound(
    *,
    conversation_id: str | None = None,
    presence_id: str | None = None,
    message_id: str = "plugin-src",
    bot_user_id: str = "8000",
    user_id: str = "1001",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="Tester"),
        text="hello",
        bot_user_id=bot_user_id,
        received_at=_NOW,
        conversation_id=conversation_id,
        presence_id=presence_id,
        legacy_conversation_key=f"bot:{bot_user_id}:private:{user_id}",
    )


def _web_response() -> WebSearchResponse:
    return WebSearchResponse(
        query="now",
        sources=(
            WebSearchSource(
                source_id="s1",
                title="Example",
                url="https://example.com/article",
                domain="example.com",
                snippet="ok",
                relevant_content="",
            ),
        ),
        provider_request_id=None,
        latency_seconds=0.1,
    )


async def _agent_runtime(
    database: Database,
    *,
    canonical_conversation_id: str | None = None,
    capabilities: frozenset[str] = frozenset({"web_search"}),
) -> AgentRuntime:
    settings = make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.TAVILY,
        tavily_api_key="test-placeholder",
    )
    snapshot = await RuntimeConfigService(settings=settings, database=database).snapshot(
        user_id="1001"
    )
    now = datetime.now(UTC)
    return AgentRuntime(
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key=f"plugin-agent:example.plugin:{uuid4()}",
        current_group_id=None,
        bot_user_id="8000",
        gateway=None,
        runtime_config=snapshot,
        current_time=TimeContext(utc=now, local=now, timezone="UTC"),
        allowed_capabilities=capabilities,
        max_tool_calls=2,
        max_model_requests=2,
        canonical_conversation_id=canonical_conversation_id,
    )


def _agent_tool_service(
    database: Database,
    provider: FakeWebSearchProvider,
) -> AgentToolService:
    settings = make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.TAVILY,
        tavily_api_key="test-placeholder",
    )
    return AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        actions=AgentActionRepository(database),
        web_provider=provider,
        web_sources=WebSearchSourceRepository(database),
        runtime_config=RuntimeConfigService(settings=settings, database=database),
    )


def _web_backend(
    database: Database,
    provider: FakeWebSearchProvider,
) -> PluginAgentToolBackend:
    return PluginAgentToolBackend(_agent_tool_service(database, provider))


class _CountingMCPConnection:
    def __init__(self, inner: FakeMCPConnection) -> None:
        self._inner = inner
        self.connect_count = 0
        self.close_count = 0

    @property
    def calls(self) -> list[tuple[str, dict[str, object]]]:
        return self._inner.calls

    @property
    def connected(self) -> bool:
        return self._inner.connected

    @property
    def server_info(self) -> dict[str, str]:
        return self._inner.server_info

    async def connect(self) -> None:
        self.connect_count += 1
        await self._inner.connect()

    async def list_tools(self) -> tuple[Any, ...]:
        return await self._inner.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
        return await self._inner.call_tool(name, arguments)

    async def close(self) -> None:
        self.close_count += 1
        await self._inner.close()

    def set_tools_changed_callback(self, callback: Any) -> None:
        self._inner.set_tools_changed_callback(callback)

    def reset_counts(self) -> None:
        self.connect_count = 0
        self.close_count = 0
        self._inner.calls.clear()


async def _start_mcp(
    database: Database,
    tmp_path: Path,
) -> tuple[MCPManager, _CountingMCPConnection]:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {"mcpServers": {"demo": {"url": "https://example.invalid/mcp", "lifecycle": "lazy"}}}
        ),
        encoding="utf-8",
    )
    sdk_tool = SimpleNamespace(
        name="echo",
        description="echo",
        inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
        outputSchema=None,
        annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
    )
    connection = _CountingMCPConnection(
        FakeMCPConnection(
            tools=(sdk_tool,),
            results={
                "echo": SimpleNamespace(
                    content=(),
                    structuredContent={"ok": True},
                    isError=False,
                )
            },
        )
    )
    manager = MCPManager(
        enabled=True,
        config_path=config_path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=2,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    await manager.ensure_metadata("demo")
    await manager.disconnect("demo")
    connection.reset_counts()
    return manager, connection


async def _delete_conversation(database: Database, conversation_id: str) -> None:
    async with database.sessions() as session, session.begin():
        aliases = list(
            await session.scalars(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.conversation_id == conversation_id
                )
            )
        )
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if conversation is not None:
            await session.delete(conversation)
        for alias in aliases:
            await session.delete(alias)


def _colliding_event(
    *,
    conversation_id: str,
    platform_message_id: str | None = None,
) -> ChatEventModel:
    return ChatEventModel(
        bot_user_id="8000",
        platform_message_id=platform_message_id or f"plugin-agent-{_COLLISION_UUID}",
        scope_type="private",
        private_peer_user_id="1001",
        sender_user_id="1001",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
        canonical_event_id=str(uuid4()),
        canonical_conversation_id=conversation_id,
        suppression_status="keeper",
    )


async def _seed_plugin_trigger_collisions(database: Database, conversation_id: str) -> None:
    async with database.sessions() as session, session.begin():
        session.add(_colliding_event(conversation_id=conversation_id))
        session.add(
            _colliding_event(
                conversation_id=conversation_id,
                platform_message_id="plugin-agent",
            )
        )


def _unreachable_event_resolver(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("event resolver unreachable when infer=false")


@contextmanager
def _forbid_event_resolution() -> Iterator[MagicMock]:
    with (
        patch(
            "qq_ai_bot.persistence.web_repository.resolve_conversation_id_for_chat_event",
            side_effect=_unreachable_event_resolver,
        ) as resolve,
        patch(
            "qq_ai_bot.identity.c24_conversation.resolve_conversation_id_for_chat_event",
            side_effect=_unreachable_event_resolver,
        ),
        patch(
            "qq_ai_bot.identity.c24_conversation.load_unique_live_chat_event",
            side_effect=_unreachable_event_resolver,
        ),
    ):
        yield resolve


def _plugin_context(manager: MCPManager) -> HostPluginContext:
    return HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MCP_CALL, PluginPermission.MCP_READ),
        services=PluginFacadeServices(mcp_manager=manager),
    )


async def _web_rows(database: Database) -> list[WebSearchRunModel]:
    async with database.sessions() as session:
        return list(await session.scalars(select(WebSearchRunModel)))


async def _tool_rows(database: Database) -> list[ToolInvocationModel]:
    async with database.sessions() as session:
        return list(await session.scalars(select(ToolInvocationModel)))


@pytest.mark.asyncio
async def test_plugin_agent_web_stamps_host_conversation(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, other_id, _, _, _ = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="other-live",
                scope_type="private",
                private_peer_user_id="1001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="hi",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=other_id,
                suppression_status="keeper",
            )
        )
    await _seed_plugin_trigger_collisions(database, other_id)
    provider = FakeWebSearchProvider(response=_web_response())
    backend = _web_backend(database, provider)
    runtime = await _agent_runtime(database, canonical_conversation_id=conversation_id)
    envelope = backend._tool_runtime(runtime)

    with _forbid_event_resolution() as resolve:
        result = await backend.execute("web_search", json.dumps({"query": "now"}), runtime)

    assert json.loads(result)["ok"] is True
    resolve.assert_not_called()
    assert envelope.inbound.message_id.startswith("plugin-agent-")
    assert envelope.inbound.conversation_id == conversation_id
    assert envelope.trigger_message_id == "plugin-agent"
    assert envelope.trigger_message_id != envelope.inbound.message_id
    assert envelope.inbound.presence_id is None
    rows = await _web_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id]
    assert rows[0].trigger_message_id == "plugin-agent"
    assert rows[0].trigger_message_id != "other-live"
    assert other_id != conversation_id


@pytest.mark.asyncio
async def test_plugin_agent_web_v1_stays_null(database: Database) -> None:
    await _flip_v2(database)
    _, other_id, _, _, _ = await _seed_pair(database)
    await _seed_plugin_trigger_collisions(database, other_id)
    provider = FakeWebSearchProvider(response=_web_response())
    backend = _web_backend(database, provider)
    runtime = await _agent_runtime(database, canonical_conversation_id=None)

    with _forbid_event_resolution() as resolve:
        result = await backend.execute("web_search", json.dumps({"query": "now"}), runtime)

    assert json.loads(result)["ok"] is True
    resolve.assert_not_called()
    rows = await _web_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [None]
    assert rows[0].trigger_message_id == "plugin-agent"


@pytest.mark.asyncio
async def test_plugin_agent_web_wrong_kind_and_presence_roll_back(
    database: Database,
) -> None:
    await _flip_v2(database)
    conversation_id, _, person_id, presence_id, _ = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        space_id = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    missing_id = str(uuid4())
    provider = FakeWebSearchProvider(response=_web_response())
    backend = _web_backend(database, provider)

    for conversation, category in (
        (person_id, CANONICAL_KIND_MISMATCH),
        (presence_id, CANONICAL_KIND_MISMATCH),
        (space_id, CANONICAL_KIND_MISMATCH),
        (missing_id, MISSING_CANONICAL_CONVERSATION),
    ):
        with pytest.raises(IdentityDualWriteError) as exc:
            await backend.execute(
                "web_search",
                json.dumps({"query": "now"}),
                await _agent_runtime(database, canonical_conversation_id=conversation),
            )
        assert exc.value.category == category

    await _delete_conversation(database, conversation_id)
    with pytest.raises(IdentityDualWriteError) as deleted_exc:
        await backend.execute(
            "web_search",
            json.dumps({"query": "now"}),
            await _agent_runtime(database, canonical_conversation_id=conversation_id),
        )

    assert deleted_exc.value.category == MISSING_CANONICAL_CONVERSATION
    assert await _web_rows(database) == []
    assert await _tool_rows(database) == []
    assert provider.search_requests == []


@pytest.mark.asyncio
async def test_plugin_agent_read_webpage_wrong_kind_zero_extracts(database: Database) -> None:
    await _flip_v2(database)
    _, _, person_id, presence_id, _ = await _seed_pair(database)
    page = _web_response().sources[0]
    provider = FakeWebSearchProvider(extracted={page.url: page})
    service = _agent_tool_service(database, provider)
    inbound = InboundMessage(
        message_id=f"plugin-agent-{_COLLISION_UUID}",
        event_type="plugin_agent",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname="Tester"),
        text=page.url,
        bot_user_id="8000",
        received_at=_NOW,
        conversation_id=person_id,
        presence_id=presence_id,
        legacy_conversation_key="bot:8000:private:1001",
    )

    with pytest.raises(IdentityDualWriteError) as exc:
        await service.execute(
            "read_webpage",
            json.dumps({"url": page.url}),
            ToolRuntime(
                inbound=inbound,
                gateway=None,
                allow_generic_onebot=False,
                conversation_key="private:1001",
                trigger_message_id="plugin-agent",
            ),
        )

    assert exc.value.category == CANONICAL_KIND_MISMATCH
    assert provider.extract_requests == []
    assert provider.search_requests == []
    assert await _web_rows(database) == []


@pytest.mark.asyncio
async def test_chat_web_still_infers_literal_plugin_agent_event(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, other_id, _, _, _ = await _seed_pair(database)
    await _seed_plugin_trigger_collisions(database, other_id)
    provider = FakeWebSearchProvider(response=_web_response())
    service = _agent_tool_service(database, provider)
    inbound = _inbound(conversation_id=None, message_id="plugin-agent")

    result = await service.execute(
        "web_search",
        json.dumps({"query": "now"}),
        ToolRuntime(
            inbound=inbound,
            gateway=None,
            allow_generic_onebot=False,
            conversation_key="private:1001",
            trigger_message_id=inbound.message_id,
        ),
    )

    assert json.loads(result)["ok"] is True
    assert inbound.event_type == "message"
    assert len(provider.search_requests) == 1
    rows = await _web_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [other_id]
    assert rows[0].canonical_conversation_id != conversation_id


@pytest.mark.asyncio
async def test_plugin_agent_web_request_shape_and_isolation(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, _, _, _, _ = await _seed_pair(database)
    provider = FakeWebSearchProvider(response=_web_response())
    backend = _web_backend(database, provider)
    runtime = await _agent_runtime(database, canonical_conversation_id=conversation_id)
    denied = await backend.execute(
        "web_search",
        json.dumps({"query": "now"}),
        await _agent_runtime(
            database,
            canonical_conversation_id=conversation_id,
            capabilities=frozenset({"get_person_memories"}),
        ),
    )

    result = await backend.execute("web_search", json.dumps({"query": "now"}), runtime)

    assert json.loads(denied)["error"] == "capability_not_allowed"
    assert json.loads(result)["ok"] is True
    assert {item.name for item in fields(WebSearchRequest)} == {
        "query",
        "topic",
        "time_range",
        "start_date",
        "end_date",
        "max_results",
        "extract_max_results",
    }
    assert "canonical_conversation_id" not in asdict(provider.search_requests[0])
    assert provider.search_requests[0].query == "now"
    rows = await _web_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id]


@pytest.mark.asyncio
async def test_direct_mcp_facade_stamps_host_conversation(
    database: Database, tmp_path: Path
) -> None:
    await _flip_v2(database)
    conversation_id, _, _, presence_id, _ = await _seed_pair(database)
    manager, connection = await _start_mcp(database, tmp_path)
    context = _plugin_context(manager)
    inbound = _inbound(conversation_id=conversation_id, presence_id=presence_id)
    invocation = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=inbound.sender.user_id,
        bot_user_id=inbound.bot_user_id,
        inbound=inbound,
    )
    try:
        with context.bind(invocation):
            result = await context.mcp.call("demo", "echo", {"q": "hi"})
    finally:
        await manager.close()

    assert result.ok is True
    assert invocation.conversation_id == conversation_id
    assert invocation.presence_id == presence_id
    assert connection.connect_count == 1
    assert connection.calls == [("echo", {"q": "hi"})]
    assert connection.close_count == 1
    assert connection.connected is False
    assert "demo" not in manager._connections
    rows = await _tool_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id]
    assert "canonical_conversation_id" not in (result.data or {})


@pytest.mark.asyncio
async def test_direct_mcp_v1_and_binding_default_none(database: Database, tmp_path: Path) -> None:
    await _flip_v2(database)
    await _seed_pair(database)
    manager, connection = await _start_mcp(database, tmp_path)
    context = _plugin_context(manager)
    v1 = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8000",
        inbound=_inbound(),
    )
    try:
        with context.bind(v1):
            plugin_result = await context.mcp.call("demo", "echo", {"q": "v1"})
        binding_result = await MCPToolBinding(
            manager,
            "demo",
            "echo",
            record_invocation=True,
        ).invoke(
            {"q": "kernel"},
            ToolInvocationContext(
                runtime=MCPPolicyRuntime(
                    origin=TurnOrigin.USER_MESSAGE,
                    actor_user_id="1001",
                    actor_is_superuser=False,
                ),
                conversation_key="v1:mcp",
                actor_user_id="1001",
            ),
        )
    finally:
        await manager.close()

    assert plugin_result.ok is True
    assert binding_result.ok is True
    assert v1.conversation_id is None
    assert [args for _name, args in connection.calls] == [{"q": "v1"}, {"q": "kernel"}]
    rows = await _tool_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [None, None]


@pytest.mark.asyncio
async def test_direct_mcp_wrong_kind_rolls_back_zero_rows(
    database: Database, tmp_path: Path
) -> None:
    await _flip_v2(database)
    _, _, person_id, presence_id, _ = await _seed_pair(database)
    manager, connection = await _start_mcp(database, tmp_path)
    context = _plugin_context(manager)
    try:
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="1001",
                bot_user_id="8000",
                inbound=_inbound(conversation_id=person_id, presence_id=presence_id),
            )
        ):
            with pytest.raises(IdentityDualWriteError) as person_exc:
                await context.mcp.call("demo", "echo", {"q": "person"})
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="1001",
                bot_user_id="8000",
                inbound=_inbound(conversation_id=presence_id, presence_id=presence_id),
            )
        ):
            with pytest.raises(IdentityDualWriteError) as presence_exc:
                await context.mcp.call("demo", "echo", {"q": "presence"})
    finally:
        await manager.close()

    assert person_exc.value.category == CANONICAL_KIND_MISMATCH
    assert presence_exc.value.category == CANONICAL_KIND_MISMATCH
    assert connection.connect_count == 0
    assert connection.calls == []
    assert connection.close_count == 0
    assert connection.connected is False
    assert "demo" not in manager._connections
    assert await _tool_rows(database) == []
    assert await _web_rows(database) == []


@pytest.mark.asyncio
async def test_direct_mcp_valid_preflight_calls_once(database: Database, tmp_path: Path) -> None:
    await _flip_v2(database)
    conversation_id, _, _, presence_id, _ = await _seed_pair(database)
    manager, connection = await _start_mcp(database, tmp_path)
    context = _plugin_context(manager)
    try:
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="1001",
                bot_user_id="8000",
                inbound=_inbound(conversation_id=conversation_id, presence_id=presence_id),
            )
        ):
            result = await context.mcp.call("demo", "echo", {"q": "once"})
    finally:
        await manager.close()

    assert result.ok is True
    assert connection.connect_count == 1
    assert connection.calls == [("echo", {"q": "once"})]
    assert connection.close_count == 1
    assert connection.connected is False
    assert "demo" not in manager._connections
    rows = await _tool_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id]


@pytest.mark.asyncio
async def test_direct_mcp_missing_or_deleted_conversation_fails_before_connection(
    database: Database, tmp_path: Path
) -> None:
    await _flip_v2(database)
    conversation_id, _, _, presence_id, _ = await _seed_pair(database)
    missing_id = str(uuid4())
    manager, connection = await _start_mcp(database, tmp_path)
    context = _plugin_context(manager)
    try:
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="1001",
                bot_user_id="8000",
                inbound=_inbound(conversation_id=missing_id, presence_id=presence_id),
            )
        ):
            with pytest.raises(IdentityDualWriteError) as missing_exc:
                await context.mcp.call("demo", "echo", {"q": "missing"})
        await _delete_conversation(database, conversation_id)
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="1001",
                bot_user_id="8000",
                inbound=_inbound(conversation_id=conversation_id, presence_id=presence_id),
            )
        ):
            with pytest.raises(IdentityDualWriteError) as deleted_exc:
                await context.mcp.call("demo", "echo", {"q": "deleted"})
    finally:
        await manager.close()

    assert missing_exc.value.category == MISSING_CANONICAL_CONVERSATION
    assert deleted_exc.value.category == MISSING_CANONICAL_CONVERSATION
    assert connection.connect_count == 0
    assert connection.calls == []
    assert connection.close_count == 0
    assert await _tool_rows(database) == []


@pytest.mark.asyncio
async def test_direct_mcp_record_failure_preserves_precedence_and_lazy_disconnect(
    database: Database, tmp_path: Path
) -> None:
    await _flip_v2(database)
    conversation_id, _, _, presence_id, _ = await _seed_pair(database)
    manager, connection = await _start_mcp(database, tmp_path)
    context = _plugin_context(manager)
    inbound = _inbound(conversation_id=conversation_id, presence_id=presence_id)

    async def _raise_identity(*_args: object, **_kwargs: object) -> None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)

    async def _raise_telemetry(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry failed")

    try:
        manager._repository.record_invocation = _raise_identity  # type: ignore[method-assign]
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id=inbound.sender.user_id,
                bot_user_id=inbound.bot_user_id,
                inbound=inbound,
            )
        ):
            with pytest.raises(IdentityDualWriteError) as identity_exc:
                await context.mcp.call("demo", "echo", {"q": "identity"})
        assert identity_exc.value.category == CANONICAL_KIND_MISMATCH
        assert connection.connect_count == 1
        assert connection.calls == [("echo", {"q": "identity"})]
        assert connection.close_count == 1
        assert connection.connected is False
        assert "demo" not in manager._connections
        assert await _tool_rows(database) == []

        connection.reset_counts()
        manager._repository.record_invocation = _raise_telemetry  # type: ignore[method-assign]
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id=inbound.sender.user_id,
                bot_user_id=inbound.bot_user_id,
                inbound=inbound,
            )
        ):
            swallowed = await context.mcp.call("demo", "echo", {"q": "retry-surface"})
        assert swallowed.ok is True
        assert connection.connect_count == 1
        assert connection.calls == [("echo", {"q": "retry-surface"})]
        assert connection.close_count == 1
        assert connection.connected is False
        assert "demo" not in manager._connections
        assert await _tool_rows(database) == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_plugin_agent_web_forced_uuid_collision_v1_stays_null(database: Database) -> None:
    conversation_id, other_id, _, _, _ = await _seed_pair(database)
    await _seed_plugin_trigger_collisions(database, other_id)
    provider = FakeWebSearchProvider(response=_web_response())
    backend = _web_backend(database, provider)
    runtime = await _agent_runtime(database, canonical_conversation_id=None)

    with (
        patch("qq_ai_bot.plugin_host.agent_backend.uuid.uuid4", return_value=_COLLISION_UUID),
        _forbid_event_resolution() as resolve,
    ):
        envelope = backend._tool_runtime(runtime)
        result = await backend.execute("web_search", json.dumps({"query": "now"}), runtime)

    assert json.loads(result)["ok"] is True
    resolve.assert_not_called()
    assert envelope.inbound.message_id == f"plugin-agent-{_COLLISION_UUID}"
    assert envelope.trigger_message_id == "plugin-agent"
    assert envelope.trigger_message_id != envelope.inbound.message_id
    rows = await _web_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [None]
    assert rows[0].trigger_message_id == "plugin-agent"
    assert other_id != conversation_id


@pytest.mark.asyncio
async def test_plugin_agent_web_forced_uuid_collision_v2_stamps_host(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, other_id, _, _, _ = await _seed_pair(database)
    await _seed_plugin_trigger_collisions(database, other_id)
    provider = FakeWebSearchProvider(response=_web_response())
    backend = _web_backend(database, provider)
    runtime = await _agent_runtime(database, canonical_conversation_id=conversation_id)

    with (
        patch("qq_ai_bot.plugin_host.agent_backend.uuid.uuid4", return_value=_COLLISION_UUID),
        _forbid_event_resolution() as resolve,
    ):
        envelope = backend._tool_runtime(runtime)
        result = await backend.execute("web_search", json.dumps({"query": "now"}), runtime)

    assert json.loads(result)["ok"] is True
    resolve.assert_not_called()
    assert envelope.inbound.message_id == f"plugin-agent-{_COLLISION_UUID}"
    assert envelope.trigger_message_id == "plugin-agent"
    assert envelope.trigger_message_id != envelope.inbound.message_id
    assert envelope.inbound.conversation_id == conversation_id
    rows = await _web_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id]
    assert rows[0].canonical_conversation_id != other_id
    assert rows[0].trigger_message_id == "plugin-agent"
    assert other_id != conversation_id


@pytest.mark.asyncio
async def test_direct_mcp_request_and_cache_invariance(database: Database, tmp_path: Path) -> None:
    await _flip_v2(database)
    conversation_id, _, _, presence_id, _ = await _seed_pair(database)
    manager, connection = await _start_mcp(database, tmp_path)
    async with database.sessions() as session:
        before = list(await session.scalars(select(MCPToolCacheModel)))
    before_hashes = {
        (row.remote_tool_name, row.metadata_hash, row.input_schema_json) for row in before
    }
    context = _plugin_context(manager)
    inbound = _inbound(conversation_id=conversation_id, presence_id=presence_id)
    try:
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id=inbound.sender.user_id,
                bot_user_id=inbound.bot_user_id,
                inbound=inbound,
            )
        ):
            first = await context.mcp.call("demo", "echo", {"q": "same"})
        with context.bind(
            PluginInvocation(
                plugin_id="example.plugin",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="1001",
                bot_user_id="8000",
                inbound=_inbound(),
            )
        ):
            second = await context.mcp.call("demo", "echo", {"q": "same"})
    finally:
        await manager.close()

    assert first.ok is True
    assert second.ok is True
    assert [args for _name, args in connection.calls] == [{"q": "same"}, {"q": "same"}]
    async with database.sessions() as session:
        after = list(await session.scalars(select(MCPToolCacheModel)))
    after_hashes = {
        (row.remote_tool_name, row.metadata_hash, row.input_schema_json) for row in after
    }
    assert before_hashes == after_hashes
    assert before_hashes
    assert all("canonical_conversation_id" not in row.input_schema_json for row in after)
    rows = await _tool_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id, None]


@pytest.mark.asyncio
async def test_mcp_permission_and_plugin_isolation(database: Database, tmp_path: Path) -> None:
    manager, _connection = await _start_mcp(database, tmp_path)
    bare = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MCP_READ,),
        services=PluginFacadeServices(mcp_manager=manager),
    )
    inbound = _inbound()
    invocation = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=inbound.sender.user_id,
        bot_user_id=inbound.bot_user_id,
        inbound=inbound,
    )
    other = PluginInvocation(
        plugin_id="other.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=inbound.sender.user_id,
        bot_user_id=inbound.bot_user_id,
        inbound=inbound,
    )
    try:
        with bare.bind(invocation), pytest.raises(PluginPermissionError):
            await bare.mcp.call("demo", "echo", {"q": "nope"})
        allowed = _plugin_context(manager)
        with pytest.raises(PluginPermissionError), allowed.bind(other):
            pass
        with pytest.raises(TypeError):
            await _MCPFacade(allowed).call(  # type: ignore[misc]
                "demo",
                "echo",
                {"q": "nope"},
                canonical_conversation_id="plugin-supplied",
            )
    finally:
        await manager.close()

    assert await _tool_rows(database) == []


def test_public_sdk_and_host_call_signatures_stay_v20() -> None:
    forbidden = _CORRELATION_NAMES
    for target in (MCPFacade.call, _MCPFacade.call, FakeMCPFacade.call):
        names = set(inspect.signature(target).parameters)
        assert names == {"self", "server_id", "tool_name", "arguments"}
        assert names.isdisjoint(forbidden)
    invoke = inspect.signature(MCPToolBinding.invoke)
    assert list(invoke.parameters)[:3] == ["self", "arguments", "context"]
    for name in _CORRELATION_NAMES:
        parameter = invoke.parameters[name]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    manager = inspect.signature(MCPManager._call_resolved_tool)
    for name in _CORRELATION_NAMES:
        parameter = manager.parameters[name]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _class_method(
    tree: ast.AST, class_name: str, method_name: str
) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef)
                    and item.name == method_name
                ):
                    return item
    raise AssertionError(f"{class_name}.{method_name} missing")


def _module_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} missing")


def _has_arg(fn: ast.AsyncFunctionDef | ast.FunctionDef, name: str) -> bool:
    return any(arg.arg == name for arg in (*fn.args.args, *fn.args.kwonlyargs))


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


def _keyword_value(call: ast.Call, name: str) -> ast.expr:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"keyword {name} missing")


def _is_runtime_canonical(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "canonical_conversation_id"
        and _attr_name(node.value) == "runtime"
    )


def _mentions(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.keyword) and node.arg == name:
        return True
    if isinstance(node, ast.arg) and node.arg == name:
        return True
    if isinstance(node, ast.Name) and node.id == name:
        return True
    if isinstance(node, ast.Attribute) and node.attr == name:
        return True
    if isinstance(node, ast.Constant) and node.value == name:
        return True
    return any(_mentions(child, name) for child in ast.iter_child_nodes(node))


def _first_row_hits(node: ast.AST) -> list[int]:
    hits: list[int] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _attr_name(child.func) == "first":
            hits.append(child.lineno)
        if isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
            if child.slice.value == 0:
                hits.append(child.lineno)
    return hits


def _record_then_lazy_disconnect(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        record_in_body = any(
            isinstance(child, ast.Call) and _attr_name(child.func) == "record_invocation"
            for stmt in node.body
            for child in ast.walk(stmt)
        )
        disconnect_in_finally = any(
            isinstance(child, ast.Call) and _attr_name(child.func) == "disconnect"
            for stmt in node.finalbody
            for child in ast.walk(stmt)
        )
        if record_in_body and disconnect_in_finally:
            return True
    return False


def test_ast_plugin_agent_threads_exact_runtime_conversation() -> None:
    tree = ast.parse((SRC_ROOT / "plugin_host" / "agent_backend.py").read_text(encoding="utf-8"))
    runtime_fn = _class_method(tree, "PluginAgentToolBackend", "_tool_runtime")
    inbound_calls = [
        node
        for node in ast.walk(runtime_fn)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "InboundMessage"
    ]
    assert len(inbound_calls) == 1
    inbound = inbound_calls[0]
    event_type = _keyword_value(inbound, "event_type")
    assert isinstance(event_type, ast.Constant) and event_type.value == "plugin_agent"
    assert _is_runtime_canonical(_keyword_value(inbound, "conversation_id"))
    message_id = _keyword_value(inbound, "message_id")
    assert isinstance(message_id, ast.JoinedStr)
    assert any(
        isinstance(part, ast.Constant) and "plugin-agent-" in str(part.value)
        for part in message_id.values
    )
    presence_value = _keyword_value(inbound, "presence_id")
    assert isinstance(presence_value, ast.Call)
    assert _attr_name(presence_value.func) == "_authoritative_presence_id"
    presence = _module_function(tree, "_authoritative_presence_id")
    assert _mentions(presence, "__dataclass_fields__")
    assert not _mentions(presence, "canonical_conversation_id")
    runtime_calls = [
        node
        for node in ast.walk(runtime_fn)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "ToolRuntime"
    ]
    assert len(runtime_calls) == 1
    trigger = _keyword_value(runtime_calls[0], "trigger_message_id")
    assert isinstance(trigger, ast.Constant) and trigger.value == "plugin-agent"
    assert _first_row_hits(runtime_fn) == []
    assert not any(
        _attr_name(node.func) in {"sha256", "hexdigest"}
        for node in ast.walk(runtime_fn)
        if isinstance(node, ast.Call)
    )


def test_ast_mcp_facade_manager_binding_forward_authoritative_ids() -> None:
    facade_tree = ast.parse((SRC_ROOT / "plugin_host" / "facades.py").read_text(encoding="utf-8"))
    manager_tree = ast.parse((SRC_ROOT / "mcp" / "manager.py").read_text(encoding="utf-8"))
    binding_tree = ast.parse((SRC_ROOT / "mcp" / "binding.py").read_text(encoding="utf-8"))
    call = _class_method(facade_tree, "_MCPFacade", "call")
    assert not any(name in {arg.arg for arg in call.args.args} for name in _CORRELATION_NAMES)
    assert not any(name in {arg.arg for arg in call.args.kwonlyargs} for name in _CORRELATION_NAMES)
    invoke_calls = [
        node
        for node in ast.walk(call)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "invoke"
    ]
    assert len(invoke_calls) == 1
    invoke_call = invoke_calls[0]
    assert _attr_name(_keyword_value(invoke_call, "canonical_conversation_id")) == "conversation_id"
    assert _attr_name(_keyword_value(invoke_call, "bot_user_id")) == "bot_user_id"
    assert _attr_name(_keyword_value(invoke_call, "ingress_presence_id")) == "presence_id"
    assert _first_row_hits(call) == []

    binding_invoke = _class_method(binding_tree, "MCPToolBinding", "invoke")
    for name in _CORRELATION_NAMES:
        assert _has_arg(binding_invoke, name)
    resolved = [
        node
        for node in ast.walk(binding_invoke)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "_call_resolved_tool"
    ]
    assert len(resolved) == 1
    for name in _CORRELATION_NAMES:
        value = _keyword_value(resolved[0], name)
        assert isinstance(value, ast.Name) and value.id == name

    execute = _class_method(manager_tree, "MCPManager", "_call_resolved_tool")
    for name in _CORRELATION_NAMES:
        assert _has_arg(execute, name)
    records = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "record_invocation"
    ]
    assert len(records) == 1
    for name in _CORRELATION_NAMES:
        assert _has_keyword(records[0], name)
    preflights = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call)
        and _attr_name(node.func) == "preflight_conversation_correlation"
    ]
    assert len(preflights) == 1
    ensures = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "_ensure_connection"
    ]
    assert len(ensures) == 1
    tool_calls = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "call_tool"
    ]
    assert len(tool_calls) == 1
    assert preflights[0].lineno < ensures[0].lineno
    assert preflights[0].lineno < tool_calls[0].lineno
    assert not any(_has_keyword(tool_calls[0], name) for name in _CORRELATION_NAMES)
    assert _record_then_lazy_disconnect(execute)
    assert _first_row_hits(execute) == []


def test_ast_gate_requires_runtime_canonical_forward() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "class PluginAgentToolBackend:",
                "    def _tool_runtime(self, runtime):",
                "        return InboundMessage(message_id='plugin-agent-x', conversation_id=None)",
            )
        )
    )
    runtime_fn = _class_method(tree, "PluginAgentToolBackend", "_tool_runtime")
    inbound = next(
        node
        for node in ast.walk(runtime_fn)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "InboundMessage"
    )
    value = _keyword_value(inbound, "conversation_id")
    assert not _is_runtime_canonical(value)


def _is_infer_trigger_test(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "infer_trigger_event"


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _attr_name(child.func) == name
    ]


def _is_plugin_agent_infer_flag(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if not any(isinstance(op, ast.NotEq) for op in node.ops):
        return False
    if not (
        isinstance(node.left, ast.Attribute)
        and node.left.attr == "event_type"
        and isinstance(node.left.value, ast.Attribute)
        and node.left.value.attr == "inbound"
    ):
        return False
    return any(
        isinstance(comp, ast.Constant) and comp.value == "plugin_agent" for comp in node.comparators
    )


def test_ast_save_response_skips_event_when_infer_false() -> None:
    tree = ast.parse((SRC_ROOT / "persistence" / "web_repository.py").read_text(encoding="utf-8"))
    fn = _class_method(tree, "WebSearchSourceRepository", "save_response")
    assert _has_arg(fn, "infer_trigger_event")
    defaults = {
        arg.arg: default
        for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True)
    }
    default = defaults["infer_trigger_event"]
    assert isinstance(default, ast.Constant) and default.value is True
    resolve_calls = _calls_named(fn, "resolve_conversation_id_for_chat_event")
    load_calls = _calls_named(fn, "load_unique_live_chat_event")
    assert resolve_calls
    assert load_calls == []
    guarded: list[ast.Call] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not _is_infer_trigger_test(node.test):
            continue
        for stmt in node.body:
            guarded.extend(_calls_named(stmt, "resolve_conversation_id_for_chat_event"))
        for stmt in node.orelse:
            assert _calls_named(stmt, "resolve_conversation_id_for_chat_event") == []
    assert {id(call) for call in resolve_calls} == {id(call) for call in guarded}


def test_ast_web_repository_preflight_is_readonly_require_live() -> None:
    tree = ast.parse((SRC_ROOT / "persistence" / "web_repository.py").read_text(encoding="utf-8"))
    fn = _class_method(tree, "WebSearchSourceRepository", "preflight_conversation_correlation")
    assert _has_arg(fn, "canonical_conversation_id")
    assert _calls_named(fn, "require_live_conversation")
    assert _calls_named(fn, "begin") == []
    assert _calls_named(fn, "resolve_conversation_id_for_chat_event") == []
    assert _calls_named(fn, "load_unique_live_chat_event") == []


def test_ast_agent_tools_infer_false_only_for_plugin_agent() -> None:
    tree = ast.parse((SRC_ROOT / "services" / "agent_tools.py").read_text(encoding="utf-8"))
    persist = _class_method(tree, "AgentToolService", "_persist_web_response")
    saves = _calls_named(persist, "save_response")
    assert len(saves) == 1
    infer = _keyword_value(saves[0], "infer_trigger_event")
    assert _is_plugin_agent_infer_flag(infer)
    assert not _mentions(infer, "trigger_message_id")
    assert not _mentions(persist, "startswith")


def test_ast_web_tools_preflight_before_provider() -> None:
    tree = ast.parse((SRC_ROOT / "services" / "agent_tools.py").read_text(encoding="utf-8"))
    search = _class_method(tree, "AgentToolService", "_web_search")
    read = _class_method(tree, "AgentToolService", "_read_webpage")
    search_preflights = _calls_named(search, "preflight_conversation_correlation")
    read_preflights = _calls_named(read, "preflight_conversation_correlation")
    searches = _calls_named(search, "search")
    extracts = _calls_named(read, "extract")
    assert len(search_preflights) == 1
    assert len(read_preflights) == 1
    assert len(searches) == 1
    assert len(extracts) == 1
    assert search_preflights[0].lineno < searches[0].lineno
    assert read_preflights[0].lineno < extracts[0].lineno
    used_url = _calls_named(read, "used_url_for_trigger")
    assert len(used_url) == 1
    assert used_url[0].lineno < read_preflights[0].lineno
