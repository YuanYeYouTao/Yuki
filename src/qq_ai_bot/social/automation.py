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
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.sandbox.client import SandboxClient, sandbox_tools
from qq_ai_bot.sandbox.environment_tools import EXECUTION_TOOLS, READ_TOOLS, SANDBOX_TOOLS
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.social.service import SocialContext, SocialService
from qq_ai_bot.social.tools import social_tool_definitions
from qq_ai_bot.workspace.service import WorkspaceService, workspace_tools
from qq_ai_bot.workspace.tools import WORKSPACE_READ_TOOLS


def automation_name(name: str) -> str:
    if name.startswith("workspace_"):
        return "workspace." + name.removeprefix("workspace_")
    if name in SANDBOX_TOOLS:
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
        read = read or tool.name in READ_TOOLS | WORKSPACE_READ_TOOLS
        send = tool.name in {"send_message", "poke_person"}
        registry.register(
            AutomationCapability(
                name=name,
                description=tool.description,
                argument_model=CapabilityArguments,
                argument_schema=tool.parameters,
                result_cacheable=tool.result_cacheable,
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
                        tool_name,
                        args,
                        conversation_id=context.canonical_conversation_id,
                        request_id=f"workspace:{context.automation_run_id}:{context.step_id}",
                    )
                    return CapabilityResult(data=result)
                if tool_name in SANDBOX_TOOLS:
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
                            "step_id": context.step_id,
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
                        if tool_name in EXECUTION_TOOLS
                        else None,
                    )
                    return CapabilityResult(data=result)
                if not context.canonical_conversation_id:
                    raise SocialError("missing_call_context")
                revalidate_authority = getattr(context, "revalidate_authority", None)
                if revalidate_authority is not None:
                    await revalidate_authority(name)
                is_self = getattr(context, "creator_kind", "person") == "self"
                social_context = SocialContext(
                    turn_id=f"automation:{context.automation_run_id}",
                    call_id=context.step_id,
                    conversation_id=context.canonical_conversation_id,
                    space_id=context.canonical_target_space_id,
                    person_refs=(
                        {"current_speaker": context.canonical_target_person_id}
                        if context.canonical_target_person_id
                        else {}
                    ),
                    origin="scheduled_automation" if is_self else "social_tool",
                    automation_run_id=context.automation_run_id if is_self else None,
                    presence_id=context.canonical_presence_id if is_self else None,
                    actor=(
                        ToolActor(
                            user_id="",
                            bot_user_id=context.bot_user_id,
                            group_id=context.current_group_id,
                            origin=TurnOrigin.SCHEDULED_AUTOMATION,
                            presence_id=context.canonical_presence_id,
                            principal_kind="self",
                            automation_run_id=context.automation_run_id,
                            conversation_id=context.canonical_conversation_id,
                            instruction=context.agent_instruction or "scheduled social delivery",
                            execution_id=(
                                f"automation:{context.automation_run_id}:{context.step_id}"
                            ),
                        )
                        if is_self
                        else None
                    ),
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
                    if tool_name == "send_message":
                        selected = args.get("target") or {}
                        if not isinstance(selected, dict):
                            raise SocialError("invalid_message_arguments")
                        kind = selected.get("kind") or (
                            "space" if context.canonical_target_space_id else "person"
                        )
                        target_args = selected
                    else:
                        kind = "person"
                        target_args = args
                    target = await self.social.target(kind, target_args, social_context)
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
                    messages_sent=(
                        int(result.get("sent_messages", 1))
                        if result.get("status") == "succeeded" and tool_name == "send_message"
                        else 0
                    ),
                )

            return invoke

        return {
            automation_name(tool.name): bind(tool.name)
            for tool in (*social_tool_definitions(), *workspace_tools(), *sandbox_tools())
        }
