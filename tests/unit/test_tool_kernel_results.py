"""Offline contracts for the 2.1 Tool Kernel and provider-neutral results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityExposure,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
    ChatToolCapabilityProvider,
    InProcessToolProvider,
    ToolExecutionResult,
    ToolInvocationCoordinator,
    ToolProviderRegistry,
    ToolResultBudgeter,
    estimate_chat_tool_tokens,
    resolve_mutation_commit,
)
from qq_ai_bot.capabilities.results import normalize_legacy_result
from qq_ai_bot.domain.messages import ChatTool, ToolCall, ToolFunction
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.services.chat import _fit_artifact_page_result
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


def _tool(name: str, description: str = "") -> ChatTool:
    return ChatTool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def test_schema_token_estimate_includes_function_envelope() -> None:
    short = _tool("x")
    described = _tool("x", "long description " * 20)
    assert estimate_chat_tool_tokens(described) > estimate_chat_tool_tokens(short)
    assert estimate_chat_tool_tokens(short) > len(json.dumps(short.parameters)) // 4


def test_as_chat_tool_declares_compact_description() -> None:
    long_description = "远端完整菜单与下单字段说明。" * 40
    compact = "查询或创建一笔订单。"
    descriptor = CapabilityDescriptor(
        canonical_name="mcp.order.create",
        model_name="create_order",
        group="mcp",
        namespace="mcp.order",
        description=long_description,
        compact_description=compact,
        input_schema={"type": "object", "properties": {"sku": {"type": "string"}}},
        output_schema={"type": "object"},
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
        trust_source=CapabilityTrustSource.PLUGIN,
        allowed_origins=frozenset(TurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=True,
        cancellable=True,
        idempotency=CapabilityIdempotency.CONDITIONAL,
    )
    declared = descriptor.as_chat_tool()
    overridden = descriptor.as_chat_tool(description=long_description)
    fallback = replace(descriptor, compact_description="").as_chat_tool()

    assert declared.description == compact
    assert long_description not in declared.description
    assert overridden.description == long_description
    assert fallback.description == long_description
    assert estimate_chat_tool_tokens(declared) < estimate_chat_tool_tokens(overridden)


def test_conditional_mutation_result_preserves_explicit_commit_state() -> None:
    lookup_only = normalize_legacy_result(
        {"ok": True, "data": {"status": "selection_required"}, "mutation_committed": False},
        provider_id="plugin",
        tool_name="conditional_send",
    )
    legacy_success = normalize_legacy_result(
        {"ok": True, "data": {"status": "sent"}},
        provider_id="plugin",
        tool_name="legacy_send",
    )

    assert lookup_only.mutation_committed is False
    assert legacy_success.mutation_committed is None
    evidence = {"source": "web_tool", "source_refs": ["source-1"], "delivery": "staged"}
    core_read = normalize_legacy_result(
        {"ok": True, "data": {}, "evidence_state": evidence},
        provider_id="core",
        tool_name="web_search",
    )
    plugin_claim = normalize_legacy_result(
        {"ok": True, "data": {}, "evidence_state": evidence},
        provider_id="plugin",
        tool_name="web_search",
    )
    assert core_read.evidence_state == evidence
    assert plugin_claim.evidence_state is None
    for provider, tool, expected in (
        ("core", "get_person_memories", "bounded read"),
        ("plugin", "get_person_memories", None),
        ("core", "web_search", None),
    ):
        normalized = normalize_legacy_result(
            {"ok": True, "data": {}, "memory_grounding_policy": "bounded read"},
            provider_id=provider,
            tool_name=tool,
        )
        assert normalized.memory_grounding_policy == expected
        assert normalized.model_payload().get("memory_grounding_policy") == expected


def test_mutation_commit_resolution_uses_explicit_result_then_descriptor_effect() -> None:
    descriptors = ChatToolCapabilityProvider(
        (
            _tool("search_memory"),
            _tool("get_my_capabilities"),
            _tool("read_tool_artifact"),
        ),
        source=CapabilityTrustSource.CORE,
    ).descriptors()
    read, capability, artifact_reader = descriptors
    assert capability.namespace_id == "kernel.authority.read"
    assert capability.exposure is CapabilityExposure.PLANNED
    assert artifact_reader.namespace_id == "kernel.artifact.read"
    assert artifact_reader.exposure is CapabilityExposure.PLANNED
    write = replace(
        read,
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )

    assert not resolve_mutation_commit(ToolExecutionResult(ok=True), read)
    assert resolve_mutation_commit(ToolExecutionResult(ok=True), write)
    assert not resolve_mutation_commit(
        ToolExecutionResult(ok=True, mutation_committed=False),
        write,
    )
    assert resolve_mutation_commit(
        ToolExecutionResult(ok=True, mutation_committed=True),
        read,
    )
    assert not resolve_mutation_commit(
        ToolExecutionResult(ok=False, mutation_committed=True),
        write,
    )


@dataclass(slots=True)
class _StaticProvider:
    provider_id: str
    items: tuple[CapabilityDescriptor, ...]

    def descriptors(self, _context: object) -> tuple[CapabilityDescriptor, ...]:
        return self.items

    async def refresh(self, *, force: bool = False) -> None:
        del force

    async def close(self) -> None:
        return None


def test_catalog_schema_token_estimate_uses_compact_envelope() -> None:
    long_description = "完整远端工具说明，不应进入声明信封。" * 30
    compact = "短说明。"
    descriptor = CapabilityDescriptor(
        canonical_name="mcp.order.create",
        model_name="create_order",
        group="mcp",
        namespace="mcp.order",
        description=long_description,
        compact_description=compact,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
        trust_source=CapabilityTrustSource.PLUGIN,
        allowed_origins=frozenset(TurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=True,
        cancellable=True,
        idempotency=CapabilityIdempotency.CONDITIONAL,
        provider_id="mcp.order",
        provider_tool_name="create-order",
    )
    registry = ToolProviderRegistry()
    registry.register(_StaticProvider("mcp.order", (descriptor,)))
    entry = registry.catalog(object()).by_model_name("create_order")
    assert entry is not None
    assert entry.estimated_schema_tokens == estimate_chat_tool_tokens(descriptor.as_chat_tool())
    assert entry.estimated_schema_tokens < estimate_chat_tool_tokens(
        descriptor.as_chat_tool(description=long_description)
    )


@pytest.mark.asyncio
async def test_catalog_selection_schema_budget_and_binding_are_provider_neutral() -> None:
    calls: list[tuple[str, str]] = []

    async def execute(name: str, arguments: str, _context: object) -> object:
        calls.append((name, arguments))
        return {"ok": True, "data": name}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _context: (
                _tool("search_chat_history", "搜索聊天历史"),
                _tool("get_person_memories", "读取人物记忆"),
            ),
            execute=execute,
        )
    )
    catalog = registry.catalog(object())
    history = catalog.by_model_name("search_chat_history")
    assert history is not None
    binding = history.descriptor.binding
    assert binding is not None
    outcome = await binding.invoke(
        {"query": "昨天"},
        SimpleNamespace(runtime=object()),
    )
    assert outcome.ok
    assert calls

    original = catalog.entries[0].descriptor
    collision_registry = ToolProviderRegistry()
    collision_registry.register(_StaticProvider("first", (original,)))
    collision_registry.register(
        _StaticProvider(
            "second",
            (
                replace(
                    original,
                    model_name="different_model_name",
                    provider_id="second",
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="duplicate canonical capability"):
        collision_registry.catalog(object())


@dataclass(slots=True)
class _BatchBackend:
    active: int = 0
    maximum_active: int = 0
    completed: list[str] = field(default_factory=list)

    def parallel_safe(self, name: str, runtime: object) -> bool:
        del runtime
        return name.startswith("read")

    async def execute(self, name: str, arguments: str, runtime: object) -> str:
        del arguments, runtime
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0.01 if name.startswith("read_slow") else 0)
        self.completed.append(name)
        self.active -= 1
        return json.dumps({"ok": True, "name": name})


@dataclass(slots=True)
class _ControlAwareBatchBackend(_BatchBackend):
    def counts_toward_limit(self, name: str, runtime: object) -> bool:
        del runtime
        return name != "set_reply_target"


@pytest.mark.asyncio
async def test_coordinator_parallelizes_safe_stretches_but_returns_original_order() -> None:
    backend = _BatchBackend()
    calls = tuple(
        ToolCall(id=name, function=ToolFunction(name=name, arguments="{}"))
        for name in ("read_slow", "read_fast", "write", "read_after")
    )
    result = await ToolInvocationCoordinator().execute_batch(
        calls,
        backend,
        object(),
        remaining_calls=20,
        max_parallel_calls=8,
    )
    assert backend.maximum_active == 2
    assert [call.id for call, _payload, _ran in result.calls] == [call.id for call in calls]
    assert result.executed_count == 4

    many = tuple(
        ToolCall(
            id=f"read-{index}",
            function=ToolFunction(name=f"read_slow_{index}", arguments="{}"),
        )
        for index in range(64)
    )
    large_result = await ToolInvocationCoordinator().execute_batch(
        many,
        backend,
        object(),
        remaining_calls=1000,
        max_parallel_calls=64,
    )
    assert large_result.executed_count == 64
    assert [call.id for call, _payload, _ran in large_result.calls] == [call.id for call in many]


@pytest.mark.asyncio
async def test_coordinator_keeps_response_controls_outside_business_call_budget() -> None:
    backend = _ControlAwareBatchBackend()
    calls = tuple(
        ToolCall(id=name, function=ToolFunction(name=name, arguments="{}"))
        for name in ("write", "set_reply_target")
    )

    result = await ToolInvocationCoordinator().execute_batch(
        calls,
        backend,
        object(),
        remaining_calls=0,
        max_parallel_calls=1,
    )

    assert result.executed_count == 0
    assert backend.completed == ["set_reply_target"]
    assert [ran for _call, _payload, ran in result.calls] == [False, True]
    assert "tool_limit_exceeded" in result.calls[0][1]


def test_coordinator_missing_call_id_returns_error_payload() -> None:
    from qq_ai_bot.capabilities.coordinator import TOOL_RESULT_MISSING, _attach_batch_results

    calls = (
        ToolCall(id="call_01_kept", function=ToolFunction(name="a", arguments="{}")),
        ToolCall(id="call_01_missing", function=ToolFunction(name="b", arguments="{}")),
    )
    ordered = _attach_batch_results(
        calls,
        results={"call_01_kept": '{"ok":true}'},
        overflow_ids=set(),
        limited='{"ok":false,"error":"tool_limit_exceeded"}',
    )
    assert ordered[0][2] is True
    assert json.loads(ordered[1][1])["error"] == TOOL_RESULT_MISSING
    assert ordered[1][2] is False


@pytest.mark.asyncio
async def test_result_budget_keeps_valid_summary_and_pages_full_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    result = ToolExecutionResult(
        ok=True,
        data=[{"value": index} for index in range(20)],
        provider_id="fake",
        tool_name="large",
    )
    rendered = await ToolResultBudgeter(
        max_characters=400,
        item_limit=3,
        artifacts=artifacts,
    ).render(result)
    payload = json.loads(rendered.text)
    assert payload["truncated"] is True
    assert payload["artifact_handle"] == rendered.artifact_id
    page = await artifacts.read(rendered.artifact_id or "", offset=0, limit=80)
    assert page is not None
    assert page["next_offset"] == 80
    assert "content" in page
    from tests.support.terminal_result_cases import check_terminal_result_recovery

    await check_terminal_result_recovery(database, tmp_path)


@pytest.mark.asyncio
async def test_artifact_reader_result_never_creates_nested_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    artifacts = ToolArtifactRepository(database, root, retention_seconds=60)
    original_handle = await artifacts.write_artifact(
        provider_id="fake",
        tool_name="large",
        content="source",
        media_type="text/plain",
    )
    result = ToolExecutionResult(
        ok=True,
        data={
            "handle": original_handle,
            "offset": 0,
            "next_offset": 2000,
            "total_characters": 2000,
            "content": "x" * 2000,
            "query_matched": None,
        },
        provider_id="artifacts",
        tool_name="read_tool_artifact",
    )

    rendered = await ToolResultBudgeter(
        max_characters=400,
        artifacts=artifacts,
    ).render(result)

    assert rendered.truncated is True
    assert rendered.artifact_id is None
    assert len(tuple(root.glob("*.json"))) == 1


def test_artifact_pages_fit_budget_and_reconstruct_without_gaps() -> None:
    source = ('中文\\"menu"\n' * 600) + "end"
    reconstructed: list[str] = []
    offset = 0
    while offset < len(source):
        raw_end = min(len(source), offset + 32_000)
        page = {
            "handle": "stable-handle",
            "offset": offset,
            "next_offset": raw_end if raw_end < len(source) else None,
            "total_characters": len(source),
            "content": source[offset:raw_end],
            "query_matched": None,
        }
        fitted = _fit_artifact_page_result(page, max_characters=700)
        assert fitted.ok is True
        assert len(json.dumps(fitted.model_payload(), ensure_ascii=False, default=str)) <= 700
        assert isinstance(fitted.data, dict)
        assert fitted.data["handle"] == "stable-handle"
        assert fitted.data["offset"] == offset
        content = fitted.data["content"]
        assert isinstance(content, str) and content
        reconstructed.append(content)
        next_offset = fitted.data["next_offset"]
        offset = len(source) if next_offset is None else int(next_offset)

    assert "".join(reconstructed) == source


@pytest.mark.asyncio
async def test_result_budget_prioritizes_payment_url_and_order_identifier() -> None:
    result = ToolExecutionResult(
        ok=True,
        data={
            "catalog": [{"description": "x" * 2000} for _ in range(20)],
            "order": {
                "orderId": "ORDER-001",
                "status": "pending_payment",
                "payH5Url": "https://example.com/pay",
            },
        },
        provider_id="fake",
        tool_name="create-order",
    )
    rendered = await ToolResultBudgeter(max_characters=600, item_limit=1).render(result)
    assert "https://example.com/pay" in rendered.text
    assert "ORDER-001" in rendered.text
    assert "pending_payment" in rendered.text
