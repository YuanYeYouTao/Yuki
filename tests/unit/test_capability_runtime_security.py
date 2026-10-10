"""R3 capability runtime security, validation, and Responses ledger tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from qq_ai_bot.automation.models import TurnOrigin
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


def _descriptor(
    name: str,
    *,
    namespace: str,
    effect: CapabilityEffect = CapabilityEffect.READ_STATE,
    risk: CapabilityRisk = CapabilityRisk.READ,
    schema: dict[str, object] | None = None,
    origins: frozenset[TurnOrigin] | None = None,
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
        allowed_origins=origins or frozenset(TurnOrigin),
        required_permissions=permissions,
        uses_external_data=False,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        schema_version=revision,
        generation=revision,
    )


def _entry(descriptor: CapabilityDescriptor) -> UnifiedToolCatalogEntry:
    return UnifiedToolCatalogEntry(
        descriptor=descriptor,
        provider_id="core",
        scope_ids=descriptor.scope_ids,
        compact_description=descriptor.description or descriptor.model_name,
        tags=descriptor.tags,
        searchable_text=descriptor.model_name,
        estimated_schema_tokens=12,
        available=True,
        revision=descriptor.schema_version,
    )


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
    from types import SimpleNamespace

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
    assert runtime.definitions()[0].parameters == schema
    assert "schema_probe" in runtime.callable_capability_ids()
    assert runtime.validate_call("schema_probe", json.dumps(payload)) == (True, None)
    _, work, tool_runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    backend = MainAgentBackend(
        chat, replace(tool_runtime, runtime_config=await chat._runtime_config.snapshot())
    )
    backend._capability_runtime = runtime
    backend._catalog = runtime.authorized_catalog
    backend._callable_tool_names = set(runtime.callable_capability_ids())
    call = ToolCall("original-schema-call", ToolFunction("schema_probe", json.dumps(payload)))
    invocation = direct_invocations((call,), SimpleNamespace(work_control=work.control))[0]

    async def invoke():
        return await backend.execute_call(invocation)

    result = await invoke_tool(work, call, invoke)
    assert json.loads(result)["data"]["compiled_input_seen"]
    assert await invoke_tool(work, call, invoke) == result
    assert dispatched == [invocation.identity.operation_id]
    assert await work.journal.effect_state(invocation.identity.operation_id) == "accepted"
    assert (await work.control.repository.get(work.control.current["id"]))["tool_calls"] == 1
    await work.control.repository.release(work.control.lease)


async def test_admin_failure_can_continue_correct_and_reuse_original_effect(
    database, tmp_path
):
    from types import SimpleNamespace

    from tests.conftest import build_harness, make_settings
    from tests.support.work_session import invoke_tool
    from tests.unit.test_tool_effect_audit import active_work

    from qq_ai_bot.admin.action_service import ActionRegistry
    from qq_ai_bot.admin.capabilities import AdminCapabilityService
    from qq_ai_bot.capabilities.invocation import direct_invocations
    from qq_ai_bot.domain.conversations import ScopeType
    from qq_ai_bot.domain.messages import (
        ChatMessage,
        InboundMessage,
        SenderIdentity,
        ToolCall,
        ToolFunction,
    )
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.agent_runner import AgentRuntime
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    env, work, original_tools = await active_work(database, tmp_path)
    actor = original_tools.actor_context
    inbound = InboundMessage(
        message_id="inbound",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(actor.user_id),
        text=actor.instruction,
        bot_user_id=actor.bot_user_id,
        group_id=actor.group_id,
        person_id=env.person,
        space_id=env.space,
        conversation_id=actor.conversation_id,
        presence_id=env.presence,
        source_event_id=actor.event_id,
        source_execution_id=actor.execution_id,
    )
    settings = make_settings(database.url, superusers_csv="10001")
    provider = FakeLLMProvider(lambda _: "配置已更新。")
    chat = build_harness(database, settings, provider).processor._chat
    admin = AdminCapabilityService(
        settings=settings,
        runtime_config=chat._runtime_config,
        actions=SimpleNamespace(registry=ActionRegistry()),
    )
    chat.set_admin_tools(admin)
    tools = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        actor_is_superuser=True,
        allow_admin_actions=True,
        runtime_config=await chat._runtime_config.snapshot(),
        conversation_key="audit",
    )
    backend = MainAgentBackend(chat, tools)
    await backend.prepare()
    context = SimpleNamespace(work_control=work.control)
    declared = backend.definitions(context, web_was_used=False)
    calls = []

    async def execute(name, arguments):
        call = ToolCall(f"admin-call-{len(calls)}", ToolFunction(name, json.dumps(arguments)))
        calls.append(call)
        invocation = direct_invocations((call,), context, manifest_revision=work.contract)[0]

        async def invoke():
            return await backend.execute_call(invocation)

        return invocation, await invoke_tool(work, call, invoke, invocation=invocation)

    _, unavailable = await execute("admin_memory_rebuild_start", {"run_id": "unconfigured"})
    assert json.loads(unavailable)["error_code"] == "RuntimeError"
    assert backend.definitions(context, web_was_used=False) == declared
    values = {"key": "agent.max_tool_calls", "scope_type": "global", "scope_id": ""}
    _, failed = await execute("admin_set_config", {**values, "value": "not-an-integer"})
    assert json.loads(failed)["error_code"] == "validation_error"
    assert backend.definitions(context, web_was_used=False) == declared
    _, read = await execute(
        "admin_get_config", {"keys": [values["key"]], "scope_type": "global", "scope_id": ""}
    )
    assert json.loads(read)["data"]["values"][0]["value"] == 32
    original, committed = await execute("admin_set_config", {**values, "value": 17})
    assert json.loads(committed)["ok"]
    assert (await chat._runtime_config.get_effective(values["key"])).value == 17
    change_id = json.loads(committed)["data"]["change_id"]
    _, changed = await execute("admin_set_config", {**values, "value": 18})
    assert json.loads(changed)["ok"]

    async def replay():
        return await backend.execute_call(original)

    assert await invoke_tool(work, original.call, replay, invocation=original) == committed
    assert (
        json.loads(await work.journal.effect_result(original.identity.operation_id))["data"][
            "change_id"
        ]
        == change_id
    )
    assert await work.journal.effect_state(original.identity.operation_id) == "accepted"
    assert (await chat._runtime_config.get_effective(values["key"])).value == 18
    _, restored = await execute("admin_set_config", {**values, "value": 17})
    assert json.loads(restored)["ok"]
    assert json.loads(restored)["data"]["change_id"] != change_id
    assert (await work.control.repository.get(work.control.current["id"]))["tool_calls"] == 6
    assert (await chat._runtime_config.get_effective(values["key"])).value == 17
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=actor.user_id,
        actor_is_superuser=True,
        delegated_authority=None,
        conversation_key=tools.conversation_key,
        current_group_id=actor.group_id,
        bot_user_id=actor.bot_user_id,
        gateway=None,
        runtime_config=tools.runtime_config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=1,
        work_control=work.control,
    )
    result = await chat.runtime.runner.run(
        (ChatMessage("user", actor.instruction),), runtime, backend
    )
    assert result.text == "配置已更新。" and work.control.accepted_ending() == "completed"
    assert len(provider.requests) == 1
    await work.control.repository.release(work.control.lease)


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
        namespace="admin.config.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
        permissions=frozenset({"superuser"}),
    )
    visible = CapabilityPolicyEngine().visible(
        (plugin,),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
            origin=TurnOrigin.USER_MESSAGE,
        ),
    )
    assert visible == ()


def test_image_turns_do_not_replace_execution_authority() -> None:
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
            origin=TurnOrigin.USER_MESSAGE,
            contains_images=True,
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


def test_read_only_origin_hides_destructive_and_writes() -> None:
    write = _descriptor(
        "memory_change",
        namespace="memory.state.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )
    read = _descriptor("web_search", namespace="web.search")
    visible = CapabilityPolicyEngine().visible(
        (write, read),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
            origin=TurnOrigin.PLUGIN_BACKGROUND,
            read_only=True,
        ),
    )
    assert [item.model_name for item in visible] == ["web_search"]


def test_destructive_is_visible_for_user_and_autonomous_not_plugin() -> None:
    destructive = _descriptor(
        "admin_execute_action",
        namespace="admin.action.write",
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
    user = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context, origin=TurnOrigin.USER_MESSAGE),
    )
    autonomous = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context, origin=TurnOrigin.AUTONOMOUS_GROUP),
    )
    plugin = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context, origin=TurnOrigin.PLUGIN_BACKGROUND),
    )
    assert [item.model_name for item in user] == ["admin_execute_action"]
    assert [item.model_name for item in autonomous] == ["admin_execute_action"]
    assert plugin == ()


def test_catalog_entry_round_trip_for_security_fixtures() -> None:
    catalog = UnifiedToolCatalog(
        entries=(_entry(_descriptor("web_search", namespace="web.search")),),
        scopes=(),
        revision="rev",
    )
    assert catalog.by_model_name("web_search") is not None
    assert catalog.by_model_name("missing") is None


def _runtime(
    *entries: UnifiedToolCatalogEntry,
    append_only: bool = True,
    memory_view: MemoryCapabilityView | None = None,
) -> TurnCapabilityRuntime:
    catalog = UnifiedToolCatalog(entries=entries, scopes=(), revision="abcd1234")
    snapshot = DescriptorRegistrySnapshot(catalog)
    return TurnCapabilityRuntime(
        registry=snapshot,
        policy_context=CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1001", is_superuser=False),
            origin=TurnOrigin.USER_MESSAGE,
            memory_view=memory_view,
        ),
    )


def test_stable_declarations_are_complete_while_execution_remains_authorized() -> None:
    runtime = _runtime(
        _entry(_descriptor("web_search", namespace="web.search")),
        _entry(_descriptor("memory_change", namespace="memory.state.write")),
        _entry(
            _descriptor(
                "admin_set_config",
                namespace="admin.config.write",
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
    # Freshly constructed: complete declaration, nothing executable yet.
    assert runtime.callable_capability_ids() == frozenset()
    assert runtime.validate_call("web_search", '{"query":"x"}') == (False, NO_LONGER_AUTHORIZED)
    runtime.initial_exposure()
    assert {tool.name for tool in runtime.definitions()} == {
        "web_search",
        "memory_change",
        "admin_set_config",
    }
    assert runtime.callable_capability_ids() == {"web_search", "memory_change"}
    assert runtime.validate_call("web_search", '{"query":"x"}') == (True, None)
    assert runtime.validate_call("admin_set_config", '{"query":"x"}') == (
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
    declared = runtime.definitions()
    assert "memory_change" in runtime.callable_capability_ids()
    runtime.sync_memory_view(_memory_view(2, hidden=("memory.state.write",)))
    # The declaration is fixed; the hidden namespace is denied at execution.
    assert runtime.definitions() == declared
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
