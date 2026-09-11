"""Explicit delegation adapter; no forged user-message runtime."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from jsonschema import Draft202012Validator

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    AutomationCapabilityRegistry,
    CapabilityArguments,
    CapabilityArgumentValidator,
    CapabilityExecutionContext,
    CapabilityHandler,
    CapabilityResult,
)
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.sandbox.client import SandboxClient, sandbox_tools
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.social.service import SocialContext, SocialService
from qq_ai_bot.social.tools import social_tool_definitions
from qq_ai_bot.workspace.service import WorkspaceService, workspace_tools


def automation_name(name: str) -> str:
    if name.startswith("workspace_"):
        return "workspace." + name.removeprefix("workspace_")
    if name in {"run_python", "get_code_run", "cancel_code_run"}:
        return "sandbox." + name
    return "social." + name


def _validator(tool: ChatTool) -> CapabilityArgumentValidator:
    def validate(value: object, allow_templates: bool) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("invalid_arguments")
        schema = deepcopy(tool.parameters)
        if allow_templates:
            properties = cast(dict[str, Any], schema.get("properties", {}))
            for key, item in value.items():
                if (
                    key in properties
                    and isinstance(item, str)
                    and item.startswith("${")
                    and item.endswith("}")
                ):
                    properties[key] = {"type": "string"}
        if next(Draft202012Validator(schema).iter_errors(value), None) is not None:
            raise ValueError("invalid_arguments")
        return dict(value)

    return validate


def register_social_automation(
    registry: AutomationCapabilityRegistry,
    handlers: dict[str, Any],
    *,
    extra_tools: tuple[ChatTool, ...] = (),
) -> None:
    for tool in (*social_tool_definitions(), *workspace_tools(), *sandbox_tools(), *extra_tools):
        name = automation_name(tool.name)
        read = tool.name in {
            "find_contacts",
            "get_group_members",
            "read_conversation_history",
            "workspace_list",
            "workspace_read",
            "get_code_run",
        }
        send = tool.name in {"send_private_message", "send_group_message", "poke_person"}
        registry.register(
            AutomationCapability(
                name=name,
                description=tool.description,
                argument_model=CapabilityArguments,
                argument_schema=tool.parameters,
                result_cacheable=tool.result_cacheable,
                model_tool_name=tool.name,
                argument_validator=_validator(tool),
                output_schema={"type": "object"},
                required_permission=PermissionLevel.USER,
                risk_class=RiskClass.READ if read else RiskClass.SEND if send else RiskClass.MUTATE,
                retry_policy=RetryPolicy.NONE,
                allowed_origins=frozenset({TurnOrigin.SCHEDULED_AUTOMATION}),
                handler=handlers.get(name),
            )
        )


class SocialAutomationAdapter:
    def __init__(
        self, social: SocialService, workspace: WorkspaceService, sandbox: SandboxClient
    ) -> None:
        self.social, self.workspace = social, workspace
        self.sandbox = sandbox

    def mapping(self) -> dict[str, Any]:
        def bind(tool_name: str) -> CapabilityHandler:
            async def invoke(
                args: dict[str, Any], context: CapabilityExecutionContext
            ) -> CapabilityResult:
                name = automation_name(tool_name)
                if (
                    name not in context.authority.allowed_capabilities
                    or context.authority.delegated_authority is None
                ):
                    raise SocialError("capability_denied")
                if tool_name.startswith("workspace_"):
                    result = await self.workspace.execute(
                        tool_name, args, conversation_id=context.canonical_conversation_id
                    )
                    return CapabilityResult(data=result)
                if tool_name in {"run_python", "get_code_run", "cancel_code_run"}:
                    from hashlib import sha256

                    result = await self.sandbox.execute(
                        tool_name,
                        args,
                        request_id=sha256(
                            f"automation:{context.automation_run_id}:{context.step_id}".encode()
                        ).hexdigest(),
                        source={
                            "conversation_id": context.canonical_conversation_id,
                            "origin": context.authority.origin.value,
                            "actor_user_id": context.authority.actor_user_id,
                            "trigger_id": context.step_id,
                            "bot_user_id": context.bot_user_id,
                            "automation_id": context.automation_id,
                            "automation_run_id": context.automation_run_id,
                            "script_hash": context.automation_script_hash,
                            "source_step_id": context.source_step_id,
                            "generation": context.conversation_generation,
                            "instruction": context.agent_instruction,
                            "context_profile": context.agent_context_profile,
                            "automation_context": context.automation_context.model_dump(
                                mode="json"
                            ),
                            "delegated_authority": context.authority.delegated_authority.model_dump(
                                mode="json"
                            ),
                            "allowed_capabilities": sorted(context.authority.allowed_capabilities),
                            "target_person_id": context.canonical_target_person_id,
                            "target_space_id": context.canonical_target_space_id,
                        }
                        if tool_name == "run_python"
                        else None,
                    )
                    return CapabilityResult(data=result)
                if not context.canonical_conversation_id:
                    raise SocialError("missing_call_context")
                social_context = SocialContext(
                    turn_id=f"automation:{context.automation_run_id}",
                    call_id=context.step_id,
                    conversation_id=context.canonical_conversation_id,
                    space_id=context.canonical_target_space_id,
                )
                if (
                    tool_name == "read_conversation_history"
                    and not context.authority.actor_is_superuser
                ):
                    from qq_ai_bot.social.history import resolve_history_target

                    selection = await resolve_history_target(self.social, args, social_context)
                    expected = (
                        context.canonical_target_person_id
                        if selection.target.kind == "person"
                        else context.canonical_target_space_id
                    )
                    if str(selection.target.id) != expected:
                        raise SocialError("delegated_target_not_allowed")
                if not context.authority.actor_is_superuser and tool_name not in {
                    "find_contacts",
                    "get_group_members",
                    "read_conversation_history",
                }:
                    if tool_name == "recall_own_message":
                        raise SocialError("delegated_target_not_allowed")
                    kind = "space" if tool_name == "send_group_message" else "person"
                    target = await self.social.target(kind, args, social_context)
                    expected = (
                        context.canonical_target_space_id
                        if kind == "space"
                        else context.canonical_target_person_id
                    )
                    group_poke = tool_name == "poke_person" and context.canonical_target_space_id
                    if group_poke:
                        if args.get("scene", "current") != "current":
                            raise SocialError("delegated_target_not_allowed")
                    elif str(target.id) != expected:
                        raise SocialError("delegated_target_not_allowed")
                    if (
                        tool_name == "poke_person"
                        and args.get("space_id")
                        and args["space_id"] != context.canonical_target_space_id
                    ):
                        raise SocialError("delegated_target_not_allowed")
                result = await self.social.execute(tool_name, args, social_context)
                if result.get("error") or result.get("status") in {"failed", "uncertain"}:
                    from qq_ai_bot.automation.executor import AutomationExecutionError

                    raise AutomationExecutionError(
                        str(result.get("error") or "social_operation_failed"),
                        uncertain=result.get("status") == "uncertain",
                    )
                return CapabilityResult(
                    data=result,
                    messages_sent=int(
                        result.get("status") == "succeeded" and tool_name.startswith("send_")
                    ),
                )

            return invoke

        return {
            automation_name(tool.name): bind(tool.name)
            for tool in (*social_tool_definitions(), *workspace_tools(), *sandbox_tools())
        }
