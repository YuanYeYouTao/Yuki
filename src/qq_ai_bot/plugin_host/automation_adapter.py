"""Map approved plugin actions into the one existing automation registry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from pydantic import BaseModel

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
    CapabilityHandler,
    CapabilityResult,
)
from qq_ai_bot.plugin_host.canonical_projection import projection_from_automation
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.facades import PluginInvocation
from qq_ai_bot.plugin_host.manifest import PluginManifest
from yuki_plugin_sdk.models import PermissionLevel as SdkPermissionLevel
from yuki_plugin_sdk.registrar import AutomationActionRegistration, ToolRegistration
from yuki_plugin_sdk.results import PluginResult

InvocationScopeFactory = Callable[
    [str, PluginInvocation],
    AbstractAsyncContextManager[object],
]


class PluginAutomationAdapter:
    def __init__(
        self,
        *,
        extensions: ExtensionRegistry,
        automation: AutomationCapabilityRegistry,
        invocation_scope: InvocationScopeFactory | None = None,
    ) -> None:
        self._extensions = extensions
        self._automation = automation
        self._invocation_scope = invocation_scope

    def activate(self, manifest: PluginManifest) -> int:
        self.deactivate(manifest.id)
        count = 0
        candidates = (
            *self._extensions.list(
                plugin_id=manifest.id,
                kind=ExtensionKind.AUTOMATION_ACTION,
            ),
            *self._extensions.list(
                plugin_id=manifest.id,
                kind=ExtensionKind.TOOL,
            ),
        )
        for item in candidates:
            registration = cast(
                AutomationActionRegistration | ToolRegistration,
                item.registration,
            )
            if item.kind is ExtensionKind.TOOL and not any(
                origin.value == TurnOrigin.SCHEDULED_AUTOMATION.value
                for origin in registration.metadata.allowed_origins
            ):
                continue
            name = f"plugin.{manifest.id}.{registration.metadata.name}"
            self._automation.register(
                AutomationCapability(
                    name=name,
                    description=registration.metadata.description,
                    argument_model=registration.input_model,
                    output_schema=registration.output_model.model_json_schema(),
                    required_permission=(
                        PermissionLevel.SUPERUSER
                        if registration.metadata.permission is SdkPermissionLevel.SUPERUSER
                        else PermissionLevel.USER
                    ),
                    risk_class=RiskClass(registration.metadata.risk.value),
                    retry_policy=RetryPolicy(registration.metadata.retry_policy.value),
                    allowed_origins=frozenset(
                        TurnOrigin(origin.value)
                        for origin in registration.metadata.allowed_origins
                        if origin.value in {item.value for item in TurnOrigin}
                    ),
                    schema_version=registration.metadata.schema_version,
                    provider_plugin_id=manifest.id,
                    provider_version=manifest.version,
                    provider_manifest_hash=manifest.manifest_hash,
                    handler=self._handler(manifest.id, registration),
                )
            )
            count += 1
        return count

    def deactivate(self, plugin_id: str) -> int:
        return self._automation.unregister_plugin(plugin_id)

    def _handler(
        self,
        plugin_id: str,
        registration: AutomationActionRegistration | ToolRegistration,
    ) -> CapabilityHandler:
        async def execute(
            arguments: dict[str, Any],
            context: CapabilityExecutionContext,
        ) -> CapabilityResult:
            model = registration.input_model.model_validate(arguments)
            scope_factory = self._invocation_scope
            if scope_factory is None:
                raise RuntimeError("plugin automation invocation scope is unavailable")
            invocation = _automation_invocation(plugin_id, context)
            from qq_ai_bot.automation.executor import AutomationExecutionError

            try:
                async with asyncio.timeout(registration.metadata.timeout_seconds):
                    async with scope_factory(plugin_id, invocation):
                        value = await registration.handler(model)
            except (TimeoutError, OSError) as exc:
                mutating = registration.metadata.risk.value != RiskClass.READ.value
                raise AutomationExecutionError(
                    "plugin_effect_unknown" if mutating else "plugin_read_failed",
                    uncertain=mutating,
                    transient=not mutating,
                ) from exc
            if isinstance(value, PluginResult):
                if not value.ok:
                    raise AutomationExecutionError(
                        value.error_code or "plugin_automation_failed",
                        uncertain=value.data.get("uncertain") is True,
                        transient=(
                            registration.metadata.risk.value == RiskClass.READ.value
                            and value.data.get("retryable") is True
                        ),
                    )
                data = dict(value.data)
                if data.get("pending") is True:
                    work_id = data.get("work_id")
                    if not isinstance(work_id, str) or not work_id:
                        raise AutomationExecutionError("plugin_pending_work_id_required")
                    return CapabilityResult(
                        data=data,
                        pending_work_id=work_id,
                        llm_calls=_count(data, "model_requests"),
                        tool_calls=_count(data, "tool_calls_used"),
                    )
                if data.get("state") in {"failed", "cancelled", "suspended"}:
                    raise AutomationExecutionError("agent_work_blocked")
            elif isinstance(value, BaseModel):
                validated = registration.output_model.model_validate(
                    value.model_dump(mode="python")
                )
                data = validated.model_dump(mode="json")
            else:
                validated = registration.output_model.model_validate(value)
                data = validated.model_dump(mode="json")
            messages_sent = int(
                registration.metadata.risk.value == RiskClass.SEND.value
                and str(data.get("status") or "").casefold() == "sent"
            )
            return CapabilityResult(
                data=data,
                llm_calls=_count(data, "model_requests"),
                tool_calls=_count(data, "tool_calls_used") if "tool_calls_used" in data else 1,
                messages_sent=_count(data, "messages_sent")
                if "messages_sent" in data
                else messages_sent,
            )

        return execute


def _automation_invocation(
    plugin_id: str,
    context: CapabilityExecutionContext,
) -> PluginInvocation:
    """Project a validated scheduled capability context into plugin authority."""

    authority = context.authority
    if authority.origin is not TurnOrigin.SCHEDULED_AUTOMATION:
        raise RuntimeError("plugin automation requires scheduled authority")
    if authority.actor_user_id != context.creator_user_id:
        raise RuntimeError("plugin automation creator does not match trusted authority")
    if authority.bot_user_id != context.bot_user_id:
        raise RuntimeError("plugin automation bot does not match trusted authority")
    delegated = authority.delegated_authority
    if delegated is None:
        raise RuntimeError("plugin automation requires delegated authority")
    if delegated.bot_user_id != authority.bot_user_id:
        raise RuntimeError("plugin automation delegation belongs to another bot")
    if delegated.current_group_id != context.current_group_id:
        raise RuntimeError("plugin automation group does not match delegated authority")
    identity = projection_from_automation(
        conversation_key=context.conversation_key,
        conversation_id=context.canonical_conversation_id,
        person_id=context.canonical_target_person_id,
        space_id=context.canonical_target_space_id,
    )
    return PluginInvocation(
        plugin_id=plugin_id,
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id=authority.actor_user_id,
        bot_user_id=authority.bot_user_id,
        delegated_authority=delegated,
        allowed_capabilities=authority.allowed_capabilities,
        web_was_used=context.web_was_used,
        gateway=cast(Any, context.gateway),
        legacy_conversation_key=identity.conversation_key,
        person_id=identity.person_id,
        space_id=identity.space_id,
        conversation_id=identity.conversation_id,
        presence_id=identity.presence_id,
        delivery_identity=f"automation:{context.automation_run_id}:{context.step_id}",
    )


__all__ = ["InvocationScopeFactory", "PluginAutomationAdapter"]


def _count(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if type(value) is not int or value < 0:
        raise ValueError("plugin usage counter must be a nonnegative integer")
    return value


async def resume_plugin_result(
    database: Any,
    work_id: str,
    definition: AutomationCapability,
    context: CapabilityExecutionContext,
    accounted: dict[str, int],
) -> CapabilityResult:
    """A returned handle transfers result ownership; never replay its handler."""
    import json

    from qq_ai_bot.automation.executor import AutomationExecutionError
    from qq_ai_bot.runtime.work_repository import WorkRepository

    row = await WorkRepository(database).get(work_id)
    if row is None:
        raise AutomationExecutionError("plugin_work_archived")
    source = json.loads(row["source_json"])
    if (
        source.get("owner") != "plugin_invocation"
        or source.get("plugin_id") != definition.provider_plugin_id
        or source.get("approval_revision") != definition.provider_manifest_hash
        or source.get("actor_user_id") != context.creator_user_id
        or row["conversation_id"] != context.canonical_conversation_id
        or row["generation"] != context.conversation_generation
    ):
        raise AutomationExecutionError("plugin_work_owner_mismatch")
    if row["state"] in {"queued", "running", "waiting_external", "waiting_user"}:
        return CapabilityResult(data={}, pending_work_id=work_id, tool_calls=0)
    if row["state"] != "completed":
        raise AutomationExecutionError("agent_work_blocked")
    return CapabilityResult(
        data={
            "text": json.loads(row["checkpoint_json"]).get("sync_result", ""),
            "work_id": work_id,
        },
        llm_calls=max(0, row["model_requests"] - accounted.get("models", 0)),
        tool_calls=max(0, row["tool_calls"] - accounted.get("tools", 0)),
    )
