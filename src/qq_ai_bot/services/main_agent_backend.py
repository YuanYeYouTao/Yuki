"""Shared main Agent tool execution, independent of its triggering adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from qq_ai_bot.admin.permission_catalog import contains_internal_capability_payload
from qq_ai_bot.capabilities import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPolicyContext,
    CapabilityRisk,
    CapabilityTrustSource,
    ToolExecutionResult,
    ToolInvocationContext,
    ToolProviderRegistry,
    ToolResultBudgeter,
    UnifiedToolCatalog,
    resolve_mutation_commit,
)
from qq_ai_bot.capabilities.catalog import DescriptorRegistrySnapshot
from qq_ai_bot.capabilities.exposure import NO_LONGER_AUTHORIZED
from qq_ai_bot.capabilities.invocation import Invocation
from qq_ai_bot.capabilities.runtime import TurnCapabilityRuntime
from qq_ai_bot.capabilities.validation import UNDECLARED_TOOL
from qq_ai_bot.domain.messages import ChatTool, ToolCall, ToolFunction
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.memory.runtime.contract import MemoryReadPolicy
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.observability import identifier_hash
from qq_ai_bot.runtime.origin import TurnOrigin as RuntimeTurnOrigin
from qq_ai_bot.services.agent_runner import AgentRuntime, AgentToolBackend
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.policies import replies_to_bot
from qq_ai_bot.services.turn_coordinator import TurnSupersededError

if TYPE_CHECKING:
    from qq_ai_bot.services.chat import AdminToolService, ChatService

logger = logging.getLogger(__name__)

_ARTIFACT_READER_NAME = "read_tool_artifact"


class UnsentFinalResponseError(LLMError):
    """A user-facing answer never reached the explicit send tool."""


_ADMIN_RETRYABLE_ERRORS = frozenset(
    {
        "invalid_json",
        "invalid_arguments",
        "validation_error",
        "unknown_capability",
        "ValueError",
    }
)


class MainAgentBackend(AgentToolBackend):
    """Preserve event-bound chat policies behind the shared model tool loop."""

    def __init__(
        self,
        service: ChatService,
        runtime: ToolRuntime,
        *,
        allowed_tools: frozenset[str] | None = None,
    ) -> None:
        self._service = service
        self._runtime = runtime
        self._allowed_tools = allowed_tools
        self._memory_session = getattr(runtime, "memory_session", None)
        self.messages_sent = 0
        self.sent_current_texts: list[str] = []
        self._send_message_attempted = False
        self._unsent_final_feedback_count = 0
        self.failed_model_requests = 0
        self.failed_tool_calls = 0
        self._tools_closed = False
        self._web_was_used = False
        self._web_calls_used = 0
        self._capability_was_used = False
        self._admin_retry_constraint: tuple[str, str] | None = None
        self._admin_terminal_failure: dict[str, object] | None = None
        self._completed_admin_mutations: set[tuple[str, str]] = set()
        self._mutation_committed = False
        self._catalog: UnifiedToolCatalog | None = None
        self._provider_registry: ToolProviderRegistry | None = None
        self._capability_runtime: TurnCapabilityRuntime | None = None
        self._callable_tool_names: set[str] = set()
        self._tool_turn_recorded = False
        self._response_sequence = 0

    def record_failure_usage(self, *, tool_calls: int, model_requests: int) -> None:
        self.failed_tool_calls = max(self.failed_tool_calls, tool_calls)
        self.failed_model_requests = max(self.failed_model_requests, model_requests)

    async def prepare(self, runtime: AgentRuntime | None = None) -> None:
        """Prepare the local authorized catalog before the first model request."""

        del runtime
        if self._capability_runtime is not None:
            return
        capability_runtime = self._install_capability_runtime()
        await capability_runtime.prepare_initial_exposure()
        self._catalog = capability_runtime.authorized_catalog

    def _memory(self) -> Any:
        return self._memory_session

    def _exclusive_write(self) -> bool:
        session = self._memory()
        return session is not None and session.exclusive_write

    def _eager_memory_read(self) -> bool:
        session = self._memory()
        if session is None:
            return False
        return session.contract.read_policy in {
            MemoryReadPolicy.EAGER,
            MemoryReadPolicy.LOCATOR_ONLY,
        }

    def _locator_open(self) -> bool:
        session = self._memory()
        return session is not None and session.locator_open

    async def confirm_memory_prompt_exposure(self) -> None:
        session = self._memory()
        if session is not None:
            await session.confirm_prompt_exposure()

    def mark_native_web_used(self) -> None:
        """Apply post-Web isolation before same-response local calls execute."""

        self._web_was_used = True

    def consume_provider_chain_restart(self) -> bool:
        """Consume a local schema-change signal; the Runner retains the submitted chain."""

        runtime = self._capability_runtime
        if runtime is None:
            return False
        return runtime.consume_provider_chain_restart()

    def work_control_allowed(self, name: str) -> bool:
        # Lifecycle tools are declared globally, but remain subject to the
        # currently executing backend's mutation and delivery restrictions.
        if name == "task_control":
            # Recording lifecycle state grants no business or send authority.
            # Even a restricted calculation must be able to answer or stop.
            return True
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return False
        return not (
            self._prompt_tools_closed()
            or self._exclusive_write()
            or self._runtime.read_only
            or self._admin_retry_constraint is not None
        )

    def work_query_allowed(self, action: str) -> bool:
        """Use the automation directory's read authority, without executing it."""
        name = "automation_get" if action == "get" else "automation_list"
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return False
        if self._prompt_tools_closed() or self._admin_retry_constraint is not None:
            return False
        request_runtime = self._request_runtime()
        if not request_runtime.allow_automation:
            return False
        try:
            _ = request_runtime.require_actor().source_key
        except (PermissionError, ValueError):
            return False
        capability_runtime = self._ensure_capability_runtime()
        arguments = '{"automation_id":1}' if action == "get" else "{}"
        permitted, _ = capability_runtime.validate_call(name, arguments)
        return permitted

    def _prompt_tools_closed(self) -> bool:
        if self._tools_closed:
            return True
        return self._runtime.tools_closed

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        del runtime
        self._web_was_used = self._web_was_used or web_was_used
        if self._runtime.tools_closed:
            self._callable_tool_names = set()
            self._log_tool_exposure((), reason="business_tools_closed")
            return ()
        capability_runtime = self._ensure_capability_runtime()
        session = self._memory()
        if session is not None:
            capability_runtime.sync_memory_view(session.capability_view())
        definitions = capability_runtime.definitions()
        if self._tools_closed:
            definitions = tuple(tool for tool in definitions if tool.name == "send_message")
        elif self._admin_retry_constraint is not None:
            definitions = tuple(
                tool
                for tool in definitions
                if tool.name in {self._admin_retry_constraint[0], "send_message"}
            )
        definitions = tuple(sorted(definitions, key=lambda tool: tool.name))
        self._callable_tool_names = set(capability_runtime.callable_capability_ids())
        if not self._tool_turn_recorded and definitions:
            self._service._tool_metrics.record_tool_enabled_turn()
            self._tool_turn_recorded = True
        self._log_tool_exposure(definitions, reason="ready")
        return definitions

    def refresh_catalog(self, runtime: AgentRuntime, *, web_was_used: bool) -> None:
        """Refresh execution policy without constructing discarded declarations."""
        del runtime
        self._web_was_used = self._web_was_used or web_was_used
        if self._runtime.tools_closed:
            self._callable_tool_names = set()
            return
        capability_runtime = self._ensure_capability_runtime()
        session = self._memory()
        if session is not None:
            capability_runtime.sync_memory_view(session.capability_view())
        self._callable_tool_names = set(capability_runtime.callable_capability_ids())
        if not self._tool_turn_recorded and self._callable_tool_names:
            self._service._tool_metrics.record_tool_enabled_turn()
            self._tool_turn_recorded = True

    def _ensure_capability_runtime(self) -> TurnCapabilityRuntime:
        if self._capability_runtime is not None:
            self._catalog = self._capability_runtime.authorized_catalog
            return self._capability_runtime
        capability_runtime = self._install_capability_runtime()
        capability_runtime.initial_exposure()
        self._catalog = capability_runtime.authorized_catalog
        return capability_runtime

    def _refresh_capability_registry(
        self,
    ) -> DescriptorRegistrySnapshot:
        request_runtime = self._request_runtime()
        self._provider_registry = self._service._build_tool_registry(
            request_runtime,
            web_was_used=self._web_was_used,
        )
        catalog = self._provider_registry.catalog(request_runtime)
        return DescriptorRegistrySnapshot(catalog)

    def _install_capability_runtime(self) -> TurnCapabilityRuntime:
        snapshot = self._refresh_capability_registry()
        session = self._memory()
        memory_view = session.capability_view() if session is not None else None
        policy_context = CapabilityPolicyContext(
            authority=AuthorityContext(
                actor_user_id=self._runtime.actor_user_id,
                is_superuser=self._runtime.actor_is_superuser,
                principal_kind=(
                    "self"
                    if self._runtime.actor_context is not None
                    and self._runtime.actor_context.principal_kind == "self"
                    else "person"
                ),
            ),
            origin=self._runtime.origin,
            contains_images=self._runtime.image_present,
            web_was_used=self._web_was_used,
            tools_closed=self._runtime.tools_closed,
            read_only=self._runtime.read_only,
            memory_view=memory_view,
            artifact_available=self._service._tool_artifacts is not None,
        )
        authority = TurnAuthority(
            actor_user_id=(
                ""
                if self._runtime.actor_context is not None
                and self._runtime.actor_context.principal_kind == "self"
                else self._runtime.actor_user_id or "unknown"
            ),
            bot_user_id=self._runtime.effective_bot_user_id or "bot",
            origin=RuntimeTurnOrigin(self._runtime.origin.value),
            permission_ceiling=frozenset({"superuser"} if self._runtime.actor_is_superuser else ()),
            delegated_authority=None,
            authority_revision=1,
            principal_kind=(
                "self"
                if self._runtime.actor_context is not None
                and self._runtime.actor_context.principal_kind == "self"
                else "person"
            ),
            initiative_run_id=self._runtime.initiative_run_id,
        )
        self._capability_runtime = TurnCapabilityRuntime(
            registry=snapshot,
            authority=authority,
            scene=self._scene_facts(),
            memory_view=memory_view,
            policy_context=policy_context,
            append_only=self._service._responses_append_only(),
        )
        return self._capability_runtime

    def _scene_facts(self) -> Any:
        from qq_ai_bot.domain.conversations import ScopeType as DomainScopeType
        from qq_ai_bot.runtime.authority import TurnSceneFacts

        inbound = self._runtime.inbound
        scope = self._runtime.effective_scope_type
        if scope is DomainScopeType.GROUP:
            return TurnSceneFacts(
                scope_type=scope,
                group_id=self._runtime.current_group_id,
                image_present=self._runtime.image_present,
                mentions_bot=inbound.mentions_bot if inbound is not None else False,
                replies_to_bot=replies_to_bot(inbound) if inbound is not None else False,
                reply_present=bool(
                    inbound is not None and (inbound.reply_text or inbound.reply_sender_user_id)
                ),
            )
        return TurnSceneFacts(
            scope_type=scope,
            group_id=None,
            image_present=self._runtime.image_present,
            mentions_bot=inbound.mentions_bot if inbound is not None else False,
            replies_to_bot=replies_to_bot(inbound) if inbound is not None else False,
            reply_present=bool(
                inbound is not None and (inbound.reply_text or inbound.reply_sender_user_id)
            ),
        )

    def _log_tool_exposure(
        self,
        definitions: tuple[ChatTool, ...],
        *,
        reason: str,
    ) -> None:
        """Log bounded capability metadata without message text or tool arguments."""

        exposed_tools = ",".join(sorted(tool.name for tool in definitions)) or "none"
        logger.info(
            "agent_tools_exposed conversation_hash=%s origin=%s "
            "tools=%s exposed_count=%d reason=%s",
            identifier_hash(self._runtime.conversation_key) or "missing",
            self._runtime.origin.value,
            exposed_tools,
            len(definitions),
            reason,
        )

    async def archive_code_result(self, text: str) -> str | None:
        """Full program result as an authorized artifact; the model sees a preview."""
        store = self._service._tool_artifacts
        config = self._runtime.runtime_config
        tooling = config.tooling if config is not None else None
        if store is None or tooling is None or not tooling.result_artifact_enabled:
            return None
        from qq_ai_bot.mcp.artifact_access import access_from_runtime
        from qq_ai_bot.runtime.work_activation import current_work_control

        active = current_work_control.get()
        request_runtime = self._request_runtime()
        handle: str = await store.write_artifact(
            provider_id="codemode",
            tool_name="execute_code",
            content=text,
            media_type="application/json",
            retention_seconds=tooling.result_artifact_retention_seconds,
            access=access_from_runtime(
                request_runtime,
                generation=active.lease.generation if active is not None else None,
            ),
        )
        return handle

    def did_use_web(self) -> bool:
        """Expose a provider-metadata-derived effect to the shared Agent loop."""

        return self._web_was_used

    def pin_web_provider(self) -> AbstractContextManager[None]:
        tools = self._service._tools
        return tools.pin_web_provider() if tools is not None else nullcontext()

    async def execute_call(self, invocation: Invocation) -> str:
        call = invocation.call
        name, arguments_json = call.function.name, call.function.arguments
        runtime: AgentRuntime = invocation.context.runtime
        if name == "send_message" and runtime.work_control is None:
            try:
                arguments = json.loads(arguments_json)
            except ValueError:
                arguments = None
            if isinstance(arguments, dict) and "work_report" in arguments:
                return json.dumps(
                    {"ok": False, "executed": False, "error": "work_report_requires_main_work"}
                )
        if name != "send_message" and self._runtime.before_model_request is not None:
            await self._runtime.before_model_request()
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return json.dumps({"ok": False, "error": "capability_not_allowed", "executed": False})
        control = runtime.work_control
        # Provider IDs are response-local for every origin. The domain receipt
        # follows the original Host operation, including ordinary direct sends.
        receipt_call_id = invocation.identity.operation_id
        if (
            name != "send_message"
            and control is not None
            and self.is_side_effecting(name, arguments_json, runtime)
            and await control.pending()
        ):
            return json.dumps(
                {"ok": False, "error": "new_input_before_execution", "executed": False}
            )
        if name == "update_short_state" and self._service.runtime.runner.main_contract is not None:
            return await self._service.runtime.runner.main_contract.state.execute(arguments_json)
        if self._runtime.tools_closed:
            return json.dumps(
                {
                    "ok": False,
                    "error": "tools_closed",
                    "detail": "本轮只声明会话前缀工具 schema，不允许真实调用。",
                },
                ensure_ascii=False,
            )
        if self._tools_closed and name != "send_message":
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "mutation_already_committed" if self._mutation_committed else "tools_closed"
                    ),
                    "detail": (
                        "本轮已有修改成功提交，后续工具调用已关闭。"
                        if self._mutation_committed
                        else "本轮工具调用已因之前的终止错误关闭。"
                    ),
                },
                ensure_ascii=False,
            )
        capability_runtime = self._capability_runtime
        if capability_runtime is not None:
            ok, error = capability_runtime.validate_call(name, arguments_json)
            if not ok and error != UNDECLARED_TOOL:
                return json.dumps(
                    {"ok": False, "error": error or NO_LONGER_AUTHORIZED},
                    ensure_ascii=False,
                )
        if (
            name not in self._callable_tool_names
            and self._service.runtime.runner.main_contract is None
        ):
            return json.dumps(
                {"ok": False, "error": "main_agent_contract_unavailable"},
                ensure_ascii=False,
            )
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        descriptor = entry.descriptor if entry is not None else None
        if descriptor is None or descriptor.binding is None:
            contract = self._service.runtime.runner.main_contract
            if contract is not None and any(
                tool.name == name for tool in await contract.definitions()
            ):
                return json.dumps(
                    {"ok": False, "error": "capability_not_allowed", "executed": False}
                )
            return json.dumps({"ok": False, "error": "unknown_capability"})
        binding = descriptor.binding
        effective_descriptor = self._effective_descriptor(call, descriptor)
        is_web_tool = effective_descriptor.namespace_id.startswith("web.")
        is_memory_read_tool = (
            effective_descriptor.namespace_id.startswith("memory.")
            and effective_descriptor.effect is CapabilityEffect.READ_STATE
        )
        is_memory_write_tool = effective_descriptor.namespace_id == "memory.state.write"
        if is_memory_write_tool and self._service.runtime.runner.main_contract is not None:
            # Enter the already-authorized write phase on invocation, not on a
            # directory lookup. Batch effect isolation is enforced by AgentRunner.
            memory_session = self._memory()
            if memory_session is not None:
                memory_session.request_exclusive_write()
        if is_memory_read_tool and not self._exclusive_write() and not self._eager_memory_read():
            self._service._tool_metrics.record_automatic_memory_read_tool_call(
                locator_fallback=self._locator_open()
            )
        config = self._runtime.runtime_config
        assert config is not None
        mutation_identity = self._mutation_identity(call)
        mutation_committed: bool | None = False
        if mutation_identity is not None and mutation_identity in self._completed_admin_mutations:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "duplicate_mutation",
                    "detail": "本轮已经成功执行过相同修改，不再重复执行。",
                },
                ensure_ascii=False,
            )
        elif is_web_tool and self._web_calls_used >= config.web.max_calls_per_turn:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "web_tool_limit_exceeded",
                    "detail": (
                        f"本轮最多执行 {config.web.max_calls_per_turn} 次联网工具，"
                        "请根据已有结果回答。"
                    ),
                },
                ensure_ascii=False,
            )
        elif (
            name != "send_message"
            and self._admin_retry_constraint is not None
            and not self._matches_retry(
                call,
                self._admin_retry_constraint,
            )
        ):
            result = json.dumps(
                {
                    "ok": False,
                    "error": "retry_scope_violation",
                    "detail": "参数修正只能重试刚才失败的同一个工具和操作。",
                },
                ensure_ascii=False,
            )
            self._tools_closed = True
        else:
            execution_runtime = self._request_runtime()
            if mutation_identity is not None and execution_runtime.turn_token is not None:
                await self._service._turn_coordinator.mark_mutation_started(
                    execution_runtime.turn_token
                )
            try:
                parsed = json.loads(arguments_json)
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict):
                result = json.dumps(
                    {"ok": False, "error": "invalid_json"},
                    ensure_ascii=False,
                )
            else:
                started = time.perf_counter()
                try:

                    async def invoke_binding() -> ToolExecutionResult:
                        from qq_ai_bot.runtime.work_activation import current_work_control

                        work = current_work_control.get()
                        if work is not None:
                            if name != "send_message":
                                await work.validate()
                            if not await work.repository.valid(work.lease):
                                raise TurnSupersededError("work activation changed")
                            if name != "send_message" and await work.pending():
                                return ToolExecutionResult(
                                    ok=False,
                                    error_code="new_input_before_execution",
                                    public_message="新要求已到达，此调用未执行，请按新要求继续。",
                                    retryable=True,
                                )
                        if name == "send_message":
                            self._send_message_attempted = True
                        return await binding.invoke(
                            {str(key): value for key, value in parsed.items()},
                            ToolInvocationContext(
                                runtime=execution_runtime,
                                call_id=receipt_call_id,
                                conversation_key=execution_runtime.conversation_key,
                                actor_user_id=execution_runtime.actor_user_id,
                                trigger_message_id=execution_runtime.trigger_message_id,
                                execution_id=execution_runtime.effective_execution_id,
                                provider_metadata={
                                    "contains_images": bool(self._runtime.image_present),
                                    "web_was_used": self._web_was_used,
                                },
                            ),
                        )

                    outcome = await self._service.run_effect(
                        None if name == "send_message" else execution_runtime.turn_snapshot,
                        invoke_binding,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = ToolExecutionResult(
                        ok=False,
                        error_code=type(exc).__name__,
                        public_message="工具执行失败",
                        retryable=False,
                        provider_id=descriptor.provider_id,
                        tool_name=descriptor.provider_tool_name or descriptor.model_name,
                    )
                mutation_committed = self._is_mutating_call(call) and resolve_mutation_commit(
                    outcome,
                    effective_descriptor,
                )
                outcome = replace(outcome, mutation_committed=mutation_committed)
                if call.function.name == "send_message" and isinstance(outcome.data, dict):
                    receipt = outcome.data
                    file = receipt.get("file")
                    caption = receipt.get("caption")
                    parts = receipt.get("parts")
                    accepted = (
                        sum(part.get("status") == "succeeded" for part in parts)
                        if isinstance(parts, list)
                        else (
                            int(file.get("status") == "succeeded")
                            + int(
                                isinstance(caption, dict) and caption.get("status") == "succeeded"
                            )
                            if isinstance(file, dict)
                            else int(receipt.get("status") == "succeeded")
                        )
                    )
                    self.messages_sent += accepted
                    inbound = self._runtime.inbound
                    expected = (
                        {"kind": "space", "id": inbound.space_id}
                        if inbound is not None and inbound.space_id
                        else {"kind": "person", "id": inbound.person_id}
                        if inbound is not None
                        else {
                            "kind": "space" if self._runtime.space_id else "person",
                            "id": self._runtime.space_id or self._runtime.person_id,
                        }
                    )
                    sent_target = receipt.get("target")
                    deliveries = (
                        parts
                        if isinstance(parts, list)
                        else [caption]
                        if isinstance(file, dict)
                        else [receipt]
                    )
                    if isinstance(sent_target, dict) and sent_target == expected:
                        # Only the confirmed ledger projection knows what survived
                        # sanitization/splitting/speech preparation and reached QQ.
                        # Raw tool arguments cannot reconstruct delivery evidence.
                        self.sent_current_texts.extend(
                            part["delivered_text"]
                            for part in deliveries
                            if isinstance(part, dict)
                            and part.get("status") == "succeeded"
                            and isinstance(part.get("delivered_text"), str)
                            and part["delivered_text"].strip()
                        )
                tooling = config.tooling
                mcp = config.mcp
                is_mcp = effective_descriptor.trust_source is CapabilityTrustSource.MCP
                result_tokens = (
                    mcp.result_token_budget
                    if is_mcp and mcp is not None and mcp.result_token_budget is not None
                    else (tooling.result_token_budget if tooling is not None else None)
                )
                result_budget = (
                    result_tokens * 4
                    if result_tokens is not None
                    else config.agent.tool_result_max_characters
                )
                item_limit = (
                    mcp.result_item_limit
                    if is_mcp and mcp is not None and mcp.result_item_limit is not None
                    else (tooling.result_item_limit if tooling is not None else None)
                )
                artifact_store = (
                    self._service._tool_artifacts
                    if tooling is not None and tooling.result_artifact_enabled
                    else None
                )
                retention_seconds = (
                    mcp.artifact_retention_seconds
                    if is_mcp and mcp is not None
                    else (
                        tooling.result_artifact_retention_seconds if tooling is not None else None
                    )
                )
                from qq_ai_bot.mcp.artifact_access import access_from_runtime
                from qq_ai_bot.runtime.work_activation import current_work_control

                active = current_work_control.get()

                budgeted = await ToolResultBudgeter(
                    max_characters=result_budget,
                    item_limit=item_limit,
                    artifacts=artifact_store,
                    artifact_retention_seconds=retention_seconds,
                    artifact_access_resolver=(
                        lambda: access_from_runtime(
                            execution_runtime,
                            generation=active.lease.generation if active is not None else None,
                        )
                    )
                    if artifact_store is not None
                    else None,
                ).render(outcome)
                result = budgeted.text
                self._service._tool_metrics.record_invocation(
                    descriptor.provider_id,
                    descriptor.provider_tool_name or descriptor.model_name,
                    outcome.ok,
                )
                if self._service._tool_invocations is not None:
                    await self._service._record_mcp_invocation(
                        runtime=execution_runtime,
                        provider_id=descriptor.provider_id,
                        tool_name=descriptor.provider_tool_name or descriptor.model_name,
                        success=outcome.ok,
                        latency_seconds=time.perf_counter() - started,
                        result_size=len(result.encode("utf-8")),
                        artifact_created=budgeted.artifact_id is not None,
                        error_category=outcome.error_code,
                        result_excerpt=result,
                        # The same Host operation the effect row and audit fence use.
                        tool_call_id=receipt_call_id,
                    )
            if contains_internal_capability_payload(result):
                self._capability_was_used = True
            if is_web_tool:
                self._web_calls_used += 1
                self._web_was_used = True
        decoded = self._service._decode_tool_result(result)
        if self._capability_runtime is not None and self.is_side_effecting(
            name, arguments_json, runtime
        ):
            self._capability_runtime.mark_side_effect()
        session = self._memory()
        if session is not None and (is_memory_write_tool or is_memory_read_tool):
            await session.observe_tool_result(name, result)
        if is_memory_write_tool and session is not None and session.exclusive_write:
            self._service._record_memory_mutation_turn_outcome(
                self._memory_mutation_outcome(decoded)
            )
        if self._is_mutating_call(call):
            if (
                not self._exclusive_write()
                and descriptor.provider_id != "admin"
                and not decoded.get("ok")
            ):
                self._admin_retry_constraint = None
                self._admin_terminal_failure = None
                return result
            if bool(decoded.get("ok")):
                self._admin_retry_constraint = None
                self._admin_terminal_failure = None
                if mutation_identity is not None and mutation_committed:
                    self._completed_admin_mutations.add(mutation_identity)
                    self._mutation_committed = True
                    if self._exclusive_write():
                        self._tools_closed = True
            elif (decoded.get("error") or decoded.get("error_code")) == "duplicate_mutation":
                # A prior identical call already committed in this turn. Keep
                # the successful result available so the model can summarize it.
                self._admin_retry_constraint = None
                self._admin_terminal_failure = None
            elif (decoded.get("error") or decoded.get("error_code")) in {
                "memory_candidate_ambiguous",
                "memory_candidate_not_found",
            }:
                self._admin_terminal_failure = None
                self._admin_retry_constraint = None
                pass
            elif bool(decoded.get("retryable")):
                self._admin_terminal_failure = None
                self._admin_retry_constraint = self._retry_identity(call)
                if self._admin_retry_constraint is None:
                    self._tools_closed = True
            elif (decoded.get("error") or decoded.get("error_code")) in _ADMIN_RETRYABLE_ERRORS:
                self._admin_terminal_failure = decoded
                self._admin_retry_constraint = self._retry_identity(call)
                if self._admin_retry_constraint is None:
                    self._tools_closed = True
            else:
                self._admin_terminal_failure = decoded
                self._tools_closed = True
        return result

    async def observe_response(self, response: Any, runtime: AgentRuntime) -> None:
        """Apply sparse own-state evidence, using the original main-turn binding."""
        from yuki_participation.self_report import SelfReport, extract_tail

        self._response_sequence += 1
        control = runtime.work_control
        if control is not None and control.lease.work_id is not None:
            return
        _, delta = extract_tail(response.content or "")
        if delta is None:
            return
        if runtime.origin is RuntimeTurnOrigin.USER_MESSAGE and self._runtime.inbound is not None:
            observe_main = getattr(self._service, "observe_main_response", None)
            if callable(observe_main):
                sequence = (
                    max(1, control.session.sequence)
                    if control is not None and control.session is not None
                    else self._response_sequence
                )
                try:
                    await observe_main(self._runtime, sequence, response, delta)
                except Exception as exc:
                    # Own-state is derived evidence. A failed checkpoint does not
                    # invalidate a delivered message or justify replaying the turn.
                    logger.warning("participation_main_hint_failed category=%s", type(exc).__name__)
            return
        if (
            not self._runtime.initiative_run_id
            or control is None
            or control.session is None
            or control.lease.work_id is not None
        ):
            return
        session = control.session
        if session.transcript is None:
            return
        report = SelfReport(
            run_ref=self._runtime.initiative_run_id,
            sequence=max(1, session.sequence),
            response_id=(
                response.provider_request_id or f"{session.transcript.chain_id}:{session.sequence}"
            ),
            at=time.time(),
            delta=delta,
        ).model_dump(mode="json")
        reports = session.progress.setdefault("self_reports", [])
        if report not in reports:
            reports.append(report)
            session.progress["self_reports"] = reports[-32:]

    @staticmethod
    def _self_main_run(runtime: AgentRuntime) -> bool:
        return (
            runtime.origin is RuntimeTurnOrigin.SELF_INITIATIVE
            or (
                runtime.origin is RuntimeTurnOrigin.SCHEDULED_AUTOMATION
                and runtime.delegated_authority is not None
                and runtime.delegated_authority.principal_kind == "self"
            )
        ) and (runtime.work_control is None or runtime.work_control.lease.work_id is None)

    def response_feedback(self, content: str, runtime: AgentRuntime) -> str | None:
        from yuki_participation.self_report import extract_tail

        body, _ = extract_tail(content)
        if (
            runtime.origin is RuntimeTurnOrigin.USER_MESSAGE
            and self._runtime.inbound is not None
            and body.strip() in {"", "NO_REPLY"}
        ):
            return None
        if self._self_main_run(runtime):
            if body.strip() in {"", "NO_REPLY"}:
                return None
            if not self._send_message_attempted and not self.messages_sent:
                if self._unsent_final_feedback_count:
                    raise UnsentFinalResponseError("self answer was not sent")
                self._unsent_final_feedback_count += 1
                return (
                    "这段最终正文是内部结果，尚未发送。需要参与当前群讨论时调用 send_message；"
                    "决定沉默则返回 NO_REPLY。已经成功的操作不要重复。"
                )
        if self._capability_was_used and contains_internal_capability_payload(content):
            return (
                "上一正文未发送：权限结果是内部执行资料。请根据实际结果继续，勿转发内部权限载荷。"
            )
        if (
            runtime.origin is RuntimeTurnOrigin.USER_MESSAGE
            and self._runtime.inbound is not None
            and body.strip()
            and not self._send_message_attempted
            and not self.messages_sent
            and (runtime.work_control is None or runtime.work_control.current is None)
        ):
            if self._unsent_final_feedback_count:
                raise UnsentFinalResponseError("user-facing answer was not sent")
            self._unsent_final_feedback_count += 1
            logger.warning("agent_unsent_final_retry origin=%s", runtime.origin.value)
            return (
                "上一段最终正文没有发送给用户。若要回复，调用 send_message(text=答复)，"
                "当前会话省略 target；不要只写最终正文，也不要重复已成功的其他工具。"
                "若你决定不回复，返回空的最终正文。"
            )
        return None

    def allow_silent_final(self, runtime: AgentRuntime) -> bool:
        if self._self_main_run(runtime):
            return True
        return (
            runtime.origin is RuntimeTurnOrigin.USER_MESSAGE
            and self._runtime.inbound is not None
            and (runtime.work_control is None or runtime.work_control.current is None)
        )

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        from yuki_participation.self_report import extract_tail

        body, _ = extract_tail(content)
        if (
            self._self_main_run(runtime)
            or (
                runtime.origin is RuntimeTurnOrigin.USER_MESSAGE
                and self._runtime.inbound is not None
            )
        ) and body.strip() == "NO_REPLY":
            return ""
        return body

    def has_visible_effects(self) -> bool:
        """An accepted send permits a text-free internal final response."""

        return self.messages_sent > 0

    def exhausted(self, runtime: AgentRuntime) -> str:
        return "这次操作的工具调用次数过多，已停止继续执行。请把请求拆小后再试。"

    @staticmethod
    def _memory_mutation_outcome(result: dict[str, object]) -> str:
        data = result.get("data")
        payload = data if isinstance(data, dict) else {}
        applied = str(payload.get("applied_operation") or "")
        outcome = str(payload.get("outcome") or "")
        error = str(result.get("error") or result.get("error_code") or "")
        if applied == "noop" or outcome in {"no_change", "deduplicated"}:
            return "noop"
        if error == "memory_candidate_ambiguous":
            return "ambiguous"
        if error == "memory_candidate_not_found":
            return "not_found"
        if result.get("mutation_committed") is True:
            return "committed"
        return "rejected"

    def _is_mutating_call(self, call: ToolCall) -> bool:
        entry = (
            self._catalog.by_model_name(call.function.name) if self._catalog is not None else None
        )
        descriptor = entry.descriptor if entry is not None else None
        admin_tools = getattr(self._service, "_admin_tools", None)
        if descriptor is not None and descriptor.provider_id == "admin" and admin_tools is not None:
            return cast("AdminToolService", admin_tools).is_mutating_call(
                call.function.name,
                call.function.arguments,
            )
        return bool(
            descriptor is not None
            and self._effective_descriptor(call, descriptor).risk is not CapabilityRisk.READ
        )

    @staticmethod
    def _effective_descriptor(
        call: ToolCall,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityDescriptor:
        """Use a gateway target descriptor for risk/commit coordination."""

        resolver = getattr(descriptor.binding, "target_descriptor", None)
        if not callable(resolver):
            return descriptor
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return descriptor
        if not isinstance(arguments, dict):
            return descriptor
        target = resolver({str(key): value for key, value in arguments.items()})
        return target or descriptor

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool:
        del runtime
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        return bool(entry is not None and entry.descriptor.parallel_safe)

    def is_side_effecting(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> bool:
        """Classify cache invalidation through the same descriptor used for execution."""
        if name == "update_short_state":
            return True

        del runtime
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        descriptor = entry.descriptor if entry is not None else None
        if descriptor is None:
            return True
        call = ToolCall(
            id="cache-classification",
            function=ToolFunction(name=name, arguments=arguments_json),
        )
        return self._effective_descriptor(call, descriptor).risk is not CapabilityRisk.READ

    def counts_toward_limit(self, name: str, runtime: AgentRuntime) -> bool:
        """Keep local response controls and Artifact reads outside the business budget."""

        del runtime
        return name != _ARTIFACT_READER_NAME

    def _mutation_identity(self, call: ToolCall) -> tuple[str, str] | None:
        if not self._is_mutating_call(call):
            return None
        entry = self._catalog.by_model_name(call.function.name) if self._catalog else None
        if call.function.name != "memory_change" and (
            entry is None or entry.descriptor.trust_source is not CapabilityTrustSource.ADMIN
        ):
            # This is a turn-local single-write grant, not effect deduplication.
            # Legitimate sends, files, MCP and plugin writes keep their original IDs.
            return None
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            normalized = call.function.arguments.strip()
        else:
            normalized = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return call.function.name, normalized

    def _retry_identity(self, call: ToolCall) -> tuple[str, str] | None:
        if not self._is_mutating_call(call):
            return None
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        operation = next(
            (
                arguments[key]
                for key in ("action", "key", "change_id", "automation_id", "id", "name")
                if key in arguments
            ),
            call.function.name,
        )
        if not isinstance(operation, (str, int)) or isinstance(operation, bool):
            return None
        return call.function.name, str(operation)

    def _matches_retry(self, call: ToolCall, expected: tuple[str, str]) -> bool:
        return self._retry_identity(call) == expected

    def _request_runtime(self) -> ToolRuntime:
        return self._runtime
