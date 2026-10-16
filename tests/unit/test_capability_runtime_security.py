"""R3 capability runtime security, validation, and Responses ledger tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from qq_ai_bot.capabilities.catalog import (
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
)
from qq_ai_bot.capabilities.exposure import (
    NO_LONGER_AUTHORIZED,
)
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.runtime import TurnCapabilityRuntime
from qq_ai_bot.capabilities.validation import (
    TOOL_INPUT_VALIDATION_FAILED,
    JsonSchemaCapabilityValidator,
)
from qq_ai_bot.runtime.contracts import MemoryCapabilityView
from qq_ai_bot.services.agent_runner import AgentRuntime


def _descriptor(
    name: str,
    *,
    namespace: str,
    effect: CapabilityEffect = CapabilityEffect.READ_STATE,
    risk: CapabilityRisk = CapabilityRisk.READ,
    schema: dict[str, object] | None = None,
    permissions: frozenset[str] = frozenset(),
    revision: str = "1",
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        canonical_name=name,
        model_name=name,
        group=namespace,
        namespace=namespace,
        input_schema=schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effect=effect,
        risk=risk,
        trust_source=CapabilityTrustSource.CORE,
        required_permissions=permissions,
        uses_external_data=False,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        schema_version=revision,
        generation=revision,
    )


def _entry(descriptor: CapabilityDescriptor) -> UnifiedToolCatalogEntry:
    return UnifiedToolCatalogEntry(descriptor=descriptor)


def test_schema_validation_rejects_invalid_arguments() -> None:
    entry = _entry(_descriptor("web_search", namespace="web.search"))
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((entry,)) == ()
    failed = validator.validate("web_search", json.dumps({"query": 1}))
    assert failed.ok is False
    assert failed.error_category == TOOL_INPUT_VALIDATION_FAILED
    ok = validator.validate("web_search", json.dumps({"query": "news"}))
    assert ok.ok is True


def test_supported_dialect_and_nested_repetition_compile_without_policy_quarantine() -> None:
    validator = JsonSchemaCapabilityValidator()
    dialect = _descriptor(
        "legacy",
        namespace="web.search",
        schema={"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
    )
    nested = _descriptor(
        "nested",
        namespace="web.search",
        schema={
            "type": "object",
            "properties": {"query": {"type": "string", "pattern": "(a+)+"}},
        },
    )
    assert validator.admit((_entry(dialect), _entry(nested))) == ()
    assert validator.validate("legacy", "{}").ok
    assert validator.validate("nested", '{"query":"aaaa"}').ok


@pytest.mark.parametrize("schema", [{"type": "unknown-type"}, {"type": "string", "pattern": "["}])
def test_actual_schema_compilation_errors_are_quarantined(schema) -> None:
    validator = JsonSchemaCapabilityValidator()
    entry = _entry(_descriptor("invalid", namespace="probe.read", schema=schema))
    assert validator.admit((entry,)) == ("invalid",)
    assert validator.validate("invalid", "{}").error_category == "capability_schema_quarantined"


@pytest.mark.parametrize("case", ["depth", "width", "long_pattern", "optional_repeat", "draft7"])
async def test_compilable_schemas_execute_once_through_real_catalog_and_original_receipt(
    database, tmp_path, case
):
    from jsonschema.validators import validator_for
    from tests.conftest import build_harness, make_settings
    from tests.support.work_session import invoke_tool
    from tests.unit.test_tool_effect_audit import active_work

    from qq_ai_bot.capabilities.binding import InProcessToolBinding
    from qq_ai_bot.capabilities.invocation import direct_invocations
    from qq_ai_bot.capabilities.results import ToolExecutionResult
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    if case == "depth":
        schema, payload = {"type": "string"}, "leaf"
        for _ in range(13):
            schema = {"type": "object", "properties": {"nested": schema}, "required": ["nested"]}
            payload = {"nested": payload}
    elif case == "width":
        schema = {
            "type": "object",
            "properties": {f"field{index}": {"type": "string"} for index in range(256)},
        }
        payload = {"field0": "read the real field"}
    elif case in {"long_pattern", "optional_repeat"}:
        pattern = "^" + "a" * 257 + "$" if case == "long_pattern" else "^(a+)?$"
        schema = {"type": "object", "properties": {"query": {"type": "string", "pattern": pattern}}}
        payload = {"query": "a" * (257 if case == "long_pattern" else 1000)}
    else:
        schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
        payload = {}
    compiler = validator_for(schema)
    compiler.check_schema(schema)
    compiler(schema).validate(payload)
    dispatched = []

    async def execute(arguments, context):
        assert arguments == payload
        dispatched.append(context.call_id)
        return ToolExecutionResult(ok=True, data={"compiled_input_seen": True})

    descriptor = replace(
        _descriptor("schema_probe", namespace="probe.read", schema=schema),
        provider_id="core",
        binding=InProcessToolBinding("core", "schema_probe", execute),
    )
    runtime = _runtime(_entry(descriptor))
    runtime.initial_exposure()
    assert runtime.descriptor("schema_probe").input_schema == schema
    assert "schema_probe" in runtime.callable_capability_ids()
    assert runtime.validate_call("schema_probe", json.dumps(payload)) == (True, None)
    _, work, tool_runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    config = await chat._runtime_config.snapshot()
    backend = MainAgentBackend(chat, replace(tool_runtime, runtime_config=config))
    actor = tool_runtime.actor_context
    agent = AgentRuntime(
        origin=actor.origin,
        actor_user_id=actor.user_id,
        actor_is_superuser=tool_runtime.actor_is_superuser,
        delegated_authority=None,
        conversation_key=tool_runtime.conversation_key,
        current_group_id=actor.group_id,
        bot_user_id=actor.bot_user_id,
        gateway=None,
        runtime_config=config,
        allowed_capabilities=frozenset(),
        max_tool_calls=config.agent.max_tool_calls,
        max_model_requests=config.agent.max_model_requests,
        work_control=work.control,
    )
    backend._capability_runtime = runtime
    backend._catalog = runtime.authorized_catalog
    backend._callable_tool_names = set(runtime.callable_capability_ids())
    call = ToolCall("original-schema-call", ToolFunction("schema_probe", json.dumps(payload)))
    invocation = direct_invocations((call,), agent)[0]

    async def invoke():
        return await backend.execute_call(invocation)

    result = await invoke_tool(work, call, invoke)
    assert json.loads(result)["data"]["compiled_input_seen"]
    assert await invoke_tool(work, call, invoke) == result
    assert dispatched == [invocation.identity.operation_id]
    assert await work.journal.effect_state(invocation.identity.operation_id) == "accepted"
    assert (await work.control.repository.get(work.control.current["id"]))["tool_calls"] == 1
    await work.control.repository.release(work.control.lease)


async def test_memory_failure_can_continue_and_reuse_original_effect(database, tmp_path):
    from tests.conftest import build_harness, make_settings
    from tests.support.work_session import invoke_tool
    from tests.unit.test_memory_mutation import _service
    from tests.unit.test_tool_effect_audit import active_work

    from qq_ai_bot.capabilities.invocation import direct_invocations
    from qq_ai_bot.domain.conversations import ScopeType
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    env, owner, tools = await active_work(database, tmp_path)
    mutations, facts, _, _ = _service(database)
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    chat._tools._memory_mutations = mutations
    tools = replace(
        tools,
        runtime_config=await chat._runtime_config.snapshot(),
        scope_type=ScopeType.GROUP,
        space_id=env.space,
        person_id=env.person,
    )
    backend = MainAgentBackend(chat, tools)
    await backend.prepare()
    actor = tools.actor_context
    context = AgentRuntime(
        origin=actor.origin,
        actor_user_id=actor.user_id,
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key=tools.conversation_key,
        current_group_id=actor.group_id,
        bot_user_id=actor.bot_user_id,
        gateway=None,
        runtime_config=tools.runtime_config,
        allowed_capabilities=frozenset(),
        max_tool_calls=None,
        max_model_requests=None,
        work_control=owner.control,
    )
    from tests.support.fixed_contract_fixture import bind_main_contract

    bind_main_contract(harness, tmp_path)
    contract = chat.runtime.runner.main_contract
    declared = await contract.model_definitions()
    backend.refresh_catalog(context)
    assert not any(tool.name.startswith("admin_") for tool in declared)
    calls = []

    async def execute(name, arguments):
        call = ToolCall(f"memory-call-{len(calls)}", ToolFunction(name, json.dumps(arguments)))
        calls.append(call)
        invocation = direct_invocations((call,), context, manifest_revision=owner.contract)[0]

        async def invoke():
            return await backend.execute_call(invocation)

        return invocation, await invoke_tool(owner, call, invoke, invocation=invocation)

    _, retired = await execute("admin_set_config", {"key": "agent.max_tool_calls", "value": 17})
    assert not json.loads(retired)["ok"]
    _, failed = await execute("memory_change", {"operation": "create"})
    assert not json.loads(failed)["ok"]
    arguments = {
        "operation": "create",
        "target": {"subject_ref": "current_speaker", "scope_type": "person"},
        "new_content": "现在住在上海",
        "memory_key": "location:home",
        "category": "location",
        "reason": "当前请求记录居住地",
    }
    original, committed = await execute("memory_change", arguments)
    result = json.loads(committed)
    assert result["ok"], result
    fact_id = result["data"]["new_fact_id"]
    _, changed = await execute(
        "memory_change",
        {
            "operation": "correct",
            "fact_id": fact_id,
            "new_content": "现在住在北京",
            "reason": "更新居住地",
        },
    )
    replacement = json.loads(changed)
    assert replacement["ok"], replacement

    async def replay():
        return await backend.execute_call(original)

    assert await invoke_tool(owner, original.call, replay, invocation=original) == committed
    assert await owner.journal.effect_state(original.identity.operation_id) == "accepted"
    fact = await facts.get_fact(replacement["data"]["new_fact_id"])
    assert fact.content == "现在住在北京"
    backend.refresh_catalog(context)
    assert await contract.model_definitions() == declared
    assert (await owner.control.repository.get(owner.control.current["id"]))["tool_calls"] == 4
    await owner.control.repository.release(owner.control.lease)


def test_remote_ref_schema_is_quarantined() -> None:
    descriptor = _descriptor(
        "unsafe",
        namespace="web.search",
        schema={"$ref": "https://example.invalid/schema.json"},
    )
    validator = JsonSchemaCapabilityValidator()
    quarantined = validator.admit((_entry(descriptor),))
    assert quarantined == ("unsafe",)
    result = validator.validate("unsafe", "{}")
    assert result.ok is False
    assert result.error_category == "capability_schema_quarantined"


def test_namespace_is_not_a_permission() -> None:
    plugin = _descriptor(
        "plugin__x__admin_set_config",
        namespace="qq.api",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
        permissions=frozenset({"superuser"}),
    )
    visible = CapabilityPolicyEngine().visible(
        (plugin,),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
        ),
    )
    assert visible == ()


def test_write_and_read_capabilities_use_actual_execution_authority() -> None:
    write = _descriptor(
        "memory_change",
        namespace="memory.state.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )
    mutate = _descriptor(
        "call_onebot_api",
        namespace="qq.platform.mutate",
        effect=CapabilityEffect.PLATFORM_MUTATE,
        risk=CapabilityRisk.MUTATE,
    )
    read = _descriptor("search_memory", namespace="memory.person.read")
    environment = tuple(
        _descriptor(
            name,
            namespace="sandbox.run",
            effect=CapabilityEffect.WRITE_STATE,
            risk=CapabilityRisk.MUTATE,
        )
        for name in ("terminal_exec", "terminal_exec", "workspace_write")
    )
    visible = CapabilityPolicyEngine().visible(
        (write, mutate, read, *environment),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
        ),
    )
    assert [item.model_name for item in visible] == [
        "memory_change",
        "call_onebot_api",
        "search_memory",
        "terminal_exec",
        "terminal_exec",
        "workspace_write",
    ]


def test_destructive_uses_actual_permissions() -> None:
    destructive = _descriptor(
        "call_onebot_api",
        namespace="qq.api",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.DESTRUCTIVE,
        permissions=frozenset({"superuser"}),
    )
    engine = CapabilityPolicyEngine()
    context = {
        "authority": AuthorityContext(
            actor_user_id="u1",
            is_superuser=True,
            permissions=frozenset({"superuser"}),
        )
    }
    allowed = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context),
    )
    refused = engine.visible(
        (destructive,),
        CapabilityPolicyContext(authority=AuthorityContext(actor_user_id="u1", is_superuser=False)),
    )
    assert [item.model_name for item in allowed] == ["call_onebot_api"]
    assert refused == ()


def test_catalog_entry_round_trip_for_security_fixtures() -> None:
    catalog = UnifiedToolCatalog(
        entries=(_entry(_descriptor("web_search", namespace="web.search")),),
    )
    assert catalog.by_model_name("web_search") is not None
    assert catalog.by_model_name("missing") is None


def _runtime(
    *entries: UnifiedToolCatalogEntry,
    append_only: bool = True,
    memory_view: MemoryCapabilityView | None = None,
) -> TurnCapabilityRuntime:
    catalog = UnifiedToolCatalog(entries=entries)
    snapshot = DescriptorRegistrySnapshot(catalog)
    return TurnCapabilityRuntime(
        registry=snapshot,
        policy_context=CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1001", is_superuser=False),
            memory_view=memory_view,
        ),
    )


def test_complete_catalog_keeps_execution_authorized() -> None:
    runtime = _runtime(
        _entry(_descriptor("web_search", namespace="web.search")),
        _entry(_descriptor("memory_change", namespace="memory.state.write")),
        _entry(
            _descriptor(
                "call_onebot_api",
                namespace="qq.api",
                permissions=frozenset({"superuser"}),
            )
        ),
        _entry(
            replace(
                _descriptor("synthetic_directory", namespace="tool.synthetic"),
                provider_metadata={"synthetic": True},
            )
        ),
    )
    # Freshly constructed: complete catalog, nothing executable yet.
    assert runtime.callable_capability_ids() == frozenset()
    assert runtime.validate_call("web_search", '{"query":"x"}') == (False, NO_LONGER_AUTHORIZED)
    runtime.initial_exposure()
    assert {entry.descriptor.capability_id for entry in runtime.authorized_catalog.entries} == {
        "web_search",
        "memory_change",
        "call_onebot_api",
    }
    assert runtime.callable_capability_ids() == {"web_search", "memory_change"}
    assert runtime.validate_call("web_search", '{"query":"x"}') == (True, None)
    assert runtime.validate_call("call_onebot_api", '{"query":"x"}') == (
        False,
        NO_LONGER_AUTHORIZED,
    )
    assert runtime.validate_call("synthetic_directory", '{"query":"x"}') == (
        False,
        "undeclared_tool",
    )


def _memory_view(revision: int, *, hidden: tuple[str, ...] = ()) -> MemoryCapabilityView:
    return MemoryCapabilityView(
        eager_namespaces=(),
        requestable_namespaces=("memory.state.write",),
        hidden_namespaces=hidden,
        transition_revision=revision,
    )


def test_memory_revision_changes_only_execution_grants_and_denial_wins() -> None:
    entries = (
        _entry(_descriptor("web_search", namespace="web.search")),
        _entry(_descriptor("memory_change", namespace="memory.state.write")),
    )
    runtime = _runtime(*entries, memory_view=_memory_view(1))
    runtime.initial_exposure()
    catalog = runtime.authorized_catalog
    assert "memory_change" in runtime.callable_capability_ids()
    runtime.sync_memory_view(_memory_view(2, hidden=("memory.state.write",)))
    # Catalog contents stay fixed; the hidden namespace is denied at execution.
    assert runtime.authorized_catalog == catalog
    assert runtime.callable_capability_ids() == {"web_search"}
    assert runtime.validate_call("memory_change", '{"query":"x"}') == (
        False,
        NO_LONGER_AUTHORIZED,
    )
    # The same revision is not re-projected.
    runtime.sync_memory_view(_memory_view(2))
    assert runtime.callable_capability_ids() == {"web_search"}


def test_memory_sync_before_initial_exposure_opens_existing_grants() -> None:
    runtime = _runtime(
        _entry(_descriptor("web_search", namespace="web.search")),
        _entry(_descriptor("memory_change", namespace="memory.state.write")),
    )
    assert runtime.callable_capability_ids() == frozenset()
    runtime.sync_memory_view(_memory_view(1))
    assert runtime.callable_capability_ids() == {"web_search", "memory_change"}
    runtime.initial_exposure()
    assert runtime.callable_capability_ids() == {"web_search", "memory_change"}
