"""R3 capability runtime security, validation, and Responses ledger tests."""

from __future__ import annotations

import json
from dataclasses import replace

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
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import MemoryCapabilityView
from qq_ai_bot.runtime.origin import TurnOrigin as RuntimeTurnOrigin


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


def test_unknown_dialect_and_unsafe_regex_are_quarantined() -> None:
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
    assert validator.admit((_entry(dialect),)) == ("legacy",)
    assert validator.admit((_entry(nested),)) == ("nested",)


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
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="9999",
            origin=RuntimeTurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
        scene=TurnSceneFacts(scope_type=ScopeType.PRIVATE, group_id=None),
        memory_view=memory_view,
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
    snapshot = runtime.initial_exposure()
    assert {tool.name for tool in runtime.definitions()} == {
        "web_search",
        "memory_change",
        "admin_set_config",
    }
    assert set(snapshot.requestable_capability_ids) == {"web_search", "memory_change"}
    assert runtime.validate_call("admin_set_config", '{"query":"x"}') == (
        False,
        NO_LONGER_AUTHORIZED,
    )
    assert runtime.validate_call("synthetic_directory", '{"query":"x"}') == (
        False,
        "undeclared_tool",
    )
