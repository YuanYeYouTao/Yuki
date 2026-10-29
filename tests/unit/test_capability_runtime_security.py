"""R3 capability runtime security, validation, and Responses ledger tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from yuki.capabilities.catalog import (
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
)
from yuki.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from yuki.capabilities.policy import CapabilityPolicyContext
from yuki.capabilities.runtime import TurnCapabilityRuntime
from yuki.capabilities.validation import (
    JsonSchemaCapabilityValidator,
)
from yuki.runtime.contracts import MemoryCapabilityView
from yuki.services.agent_runner import AgentRuntime


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


@pytest.mark.parametrize("case", ["depth", "width", "long_pattern", "optional_repeat", "draft7"])
async def test_compilable_schemas_execute_once_through_real_catalog_and_original_receipt(
    database, tmp_path, case
):
    from jsonschema.validators import validator_for
    from tests.conftest import build_harness, make_settings
    from tests.support.work_session import invoke_tool
    from tests.unit.test_tool_effect_audit import active_work

    from yuki.capabilities.binding import InProcessToolBinding
    from yuki.capabilities.invocation import direct_invocations
    from yuki.capabilities.results import ToolExecutionResult
    from yuki.domain.messages import ToolCall, ToolFunction
    from yuki.services.main_agent_backend import MainAgentBackend

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
        dispatched.append(context.invocation_key)
        return ToolExecutionResult(ok=True, data={"compiled_input_seen": True})

    descriptor = replace(
        _descriptor("schema_probe", namespace="probe.read", schema=schema),
        provider_id="core",
        binding=InProcessToolBinding("core", "schema_probe", execute),
    )
    runtime = _runtime(_entry(descriptor))
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

    from yuki.capabilities.invocation import direct_invocations
    from yuki.domain.conversations import ScopeType
    from yuki.domain.messages import ToolCall, ToolFunction
    from yuki.services.main_agent_backend import MainAgentBackend

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


def _runtime(
    *entries: UnifiedToolCatalogEntry,
    memory_view: MemoryCapabilityView | None = None,
) -> TurnCapabilityRuntime:
    catalog = UnifiedToolCatalog(entries=entries)
    snapshot = DescriptorRegistrySnapshot(catalog)
    validator = JsonSchemaCapabilityValidator()
    validator.admit(entries)
    return TurnCapabilityRuntime(
        registry=snapshot,
        schemas=validator.freeze(),
        policy_context=CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1001", is_superuser=False),
            memory_view=memory_view,
        ),
    )
