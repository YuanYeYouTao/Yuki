"""Shared main Agent tool execution, independent of its triggering adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from qq_ai_bot.capabilities import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPolicyContext,
    CapabilityRisk,
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
from qq_ai_bot.capabilities.models import CapabilityTrustSource
from qq_ai_bot.capabilities.runtime import TurnCapabilityRuntime
from qq_ai_bot.capabilities.validation import UNDECLARED_TOOL
from qq_ai_bot.domain.messages import ChatImage, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.memory.runtime.contract import MemoryReadPolicy
from qq_ai_bot.runtime.observability import identifier_hash
from qq_ai_bot.runtime.origin import TurnOrigin as RuntimeTurnOrigin
from qq_ai_bot.services.agent_runner import AgentRuntime, AgentToolBackend
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.turn_coordinator import TurnSupersededError

if TYPE_CHECKING:
    from qq_ai_bot.services.chat import AdminToolService, ChatService

logger = logging.getLogger(__name__)

_ARTIFACT_READER_NAME = "read_tool_artifact"


_ADMIN_RETRYABLE_ERRORS = frozenset(
    {"invalid_json", "invalid_arguments", "validation_error", "unknown_capability", "ValueError"}
)


class MainAgentBackend(AgentToolBackend):
    """Preserve event-bound chat policies behind the shared model tool loop."""

    @property
    def media_max_bytes(self) -> int:
        return self._service._settings.vision_max_prepared_bytes

    async def validate_images(self, images: tuple[ChatImage, ...], runtime: AgentRuntime) -> None:
        """Recheck selected tool sources after model admission, before dispatch."""
        selected = tuple(dict.fromkeys(images))
        workspace = tuple(image for image in selected if image.source in {"history", "workspace"})
        if workspace:
            if any(
                image.source == "history"
                and image.conversation_id != runtime.canonical_conversation_id
                for image in workspace
            ):
                raise LLMError("attachment_scope_denied")
            service = self._service._tools.workspace_service
            if service is None:
                raise LLMError("workspace_unavailable")
            await service.validate_images(workspace)
        external = tuple(image for image in selected if image.source == "tool")
        if external:
            plugin_images = tuple(image for image in external if image.plugin_id is not None)
            if plugin_images:
                plugin_tools = self._service._plugin_tools
                if plugin_tools is None:
                    raise LLMError("plugin_media_source_validator_unavailable")
                await plugin_tools.validate_images(
                    plugin_images, self._runtime, web_was_used=self._web_was_used
                )
            from qq_ai_bot.tool_results.access import access_from_runtime

            store = self._service._tool_artifacts
            validator = getattr(store, "validate_media", None)
            if not callable(validator):
                raise LLMError("tool_media_source_validator_unavailable")
            control = runtime.work_control
            await validator(
                external,
                access_from_runtime(
                    self._runtime,
                    generation=control.lease.generation if control is not None else None,
                ),
            )

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
        self.failed_model_requests = 0
        self.failed_tool_calls = 0
        self._tools_closed = False
        self._web_was_used = False
        self._web_calls_used = 0
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
        capability_runtime.initial_exposure()
        self._catalog = capability_runtime.authorized_catalog

    def _memory(self) -> Any:
        return self._memory_session

    def _eager_memory_read(self) -> bool:
        session = self._memory()
        if session is None:
            return False
        return session.contract.read_policy in {
            MemoryReadPolicy.EAGER,
        }

    def mark_native_web_used(self) -> None:
        """Apply post-Web isolation before same-response local calls execute."""

        self._web_was_used = True

    async def protect_native_dispatch(self, runtime: AgentRuntime) -> None:
        """Native effects cannot be ruled out after a transport cancellation."""
        execution_runtime = self._request_runtime()
        if execution_runtime.turn_token is not None:
            await self._service._turn_coordinator.mark_mutation_started(
                execution_runtime.turn_token
            )

    def work_control_allowed(self, name: str) -> bool:
        # Lifecycle tools are declared globally, but remain subject to the
        # currently executing backend's mutation and delivery restrictions.
        if self._child_work():
            # A child's lifecycle controls are exactly its frozen contract;
            # spawn/control names are absent from it, so no recursion exists.
            return self._allowed_tools is not None and name in self._allowed_tools
        if name == "task_control":
            # Recording lifecycle state grants no business or send authority.
            # Even a restricted calculation must be able to answer or stop.
            return True
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return False
        return not (self._prompt_tools_closed() or self._runtime.read_only)

    def work_query_allowed(self, action: str) -> bool:
        """Use the automation directory's read authority, without executing it.

        A child Work reads only its own lifecycle: WorkQueries fences the
        query by the child lease's work ownership, so it neither needs nor
        inherits the root's global Automation read authority.
        """
        if self._child_work():
            return (
                action in {"get", "list"}
                and self._allowed_tools is not None
                and "task_control" in self._allowed_tools
            )
        name = "automation_get" if action == "get" else "automation_list"
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return False
        if self._prompt_tools_closed():
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

    @staticmethod
    def _child_work(runtime: AgentRuntime | None = None) -> bool:
        """A persistent child lease is a trusted fact, never a constructor flag."""
        control = getattr(runtime, "work_control", None) if runtime is not None else None
        if control is None:
            from qq_ai_bot.runtime.work_activation import current_work_control

            control = current_work_control.get()
        return control is not None and control.lease.work_id is not None

    def _prompt_tools_closed(self) -> bool:
        if self._tools_closed:
            return True
        return self._runtime.tools_closed

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        child = self._child_work(runtime)
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
        if child:
            # Root/plugin declarations stay fixed; only a child catalog is its
            # frozen execution subset.
            definitions = tuple(
                tool
                for tool in definitions
                if self._allowed_tools is not None and tool.name in self._allowed_tools
            )
        if self._tools_closed:
            definitions = tuple(tool for tool in definitions if tool.name == "send_message")
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
        # The real execution backend is installed only for a consistent source.
        self._runtime.validate_source()
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
        self._capability_runtime = TurnCapabilityRuntime(
            registry=snapshot,
            policy_context=policy_context,
        )
        return self._capability_runtime

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
        from qq_ai_bot.runtime.work_activation import current_work_control
        from qq_ai_bot.tool_results.access import access_from_runtime

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
        if self._child_work(runtime) and (
            self._allowed_tools is None or name not in self._allowed_tools
        ):
            # A child's frozen tool contract is its execution ceiling. Refuse
            # before any source validation or binding, in the original shape.
            return _worker_tool_not_declared(name)
        if name != "send_message" and self._runtime.before_model_request is not None:
            await self._runtime.before_model_request()
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return _refused_result(name, "capability_not_allowed")
        contract = self._service.runtime.runner.main_contract
        if contract is not None and not contract.plugin_binding_current(name):
            return _refused_result(name, "plugin_tool_contract_changed", restart_required=True)
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
            return _refused_result(name, "new_input_before_execution")
        if name == "update_short_state" and self._service.runtime.runner.main_contract is not None:
            outcome = await self._service.runtime.runner.main_contract.state.execute(arguments_json)
            return (await ToolResultBudgeter(max_characters=None).render(outcome)).text
        if self._runtime.tools_closed:
            return _refused_result(
                name, "tools_closed", detail="本轮只声明会话前缀工具 schema，不允许真实调用。"
            )
        if self._tools_closed and name != "send_message":
            return _refused_result(
                name,
                "mutation_already_committed" if self._mutation_committed else "tools_closed",
                detail="本轮已有修改成功提交，后续工具调用已关闭。"
                if self._mutation_committed
                else "本轮工具调用已因之前的终止错误关闭。",
            )
        capability_runtime = self._capability_runtime
        if capability_runtime is not None:
            validation = capability_runtime.validate_call_result(name, arguments_json)
            if not validation.ok and validation.error_category != UNDECLARED_TOOL:
                return _refused_result(
                    name,
                    validation.error_category or NO_LONGER_AUTHORIZED,
                    detail=validation.detail,
                )
        if (
            name not in self._callable_tool_names
            and self._service.runtime.runner.main_contract is None
        ):
            return _refused_result(name, "main_agent_contract_unavailable")
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        descriptor = entry.descriptor if entry is not None else None
        if descriptor is None or descriptor.binding is None:
            contract = self._service.runtime.runner.main_contract
            if contract is not None and any(
                tool.name == name for tool in await contract.definitions()
            ):
                return _refused_result(name, "capability_not_allowed")
            return _refused_result(name, "unknown_capability")
        binding = descriptor.binding
        effective_descriptor = self._effective_descriptor(call, descriptor)
        is_web_tool = effective_descriptor.namespace_id.startswith("web.")
        is_memory_read_tool = (
            effective_descriptor.namespace_id.startswith("memory.")
            and effective_descriptor.effect is CapabilityEffect.READ_STATE
        )
        if is_memory_read_tool and not self._eager_memory_read():
            self._service._tool_metrics.record_automatic_memory_read_tool_call(
                locator_fallback=False
            )
        config = self._runtime.runtime_config
        assert config is not None
        mutation_identity = self._mutation_identity(call)
        mutation_committed: bool | None = False
        if mutation_identity is not None and mutation_identity in self._completed_admin_mutations:
            result = _refused_result(
                name, "duplicate_mutation", detail="本轮已经成功执行过相同修改，不再重复执行。"
            )
        elif is_web_tool and self._web_calls_used >= config.web.max_calls_per_turn:
            result = _refused_result(
                name,
                "web_tool_limit_exceeded",
                detail=f"本轮最多执行 {config.web.max_calls_per_turn} 次联网工具，"
                "请根据已有结果回答。",
            )
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
                result = _refused_result(name, "invalid_json")
            else:
                started = time.perf_counter()
                binding_started = False
                try:

                    async def invoke_binding() -> ToolExecutionResult:
                        nonlocal binding_started
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
                                    data={"executed": False},
                                    error_code="new_input_before_execution",
                                    public_message="新要求已到达，此调用未执行，请按新要求继续。",
                                    retryable=True,
                                    mutation_committed=False,
                                )
                        binding_started = True
                        return await binding.invoke(
                            {str(key): value for key, value in parsed.items()},
                            ToolInvocationContext(
                                runtime=execution_runtime, call_id=receipt_call_id
                            ),
                        )

                    outcome = await self._service.run_effect(
                        None if name == "send_message" else execution_runtime.turn_snapshot,
                        invoke_binding,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    uncertain = binding_started and effective_descriptor.effect not in {
                        CapabilityEffect.READ_STATE,
                        CapabilityEffect.EXTERNAL_READ,
                    }
                    outcome = ToolExecutionResult(
                        ok=False,
                        error_code=type(exc).__name__,
                        public_message="工具执行失败",
                        retryable=False,
                        uncertain=uncertain,
                        mutation_committed=None if uncertain else False,
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
                        # sanitization/splitting/media preparation and reached QQ.
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
                result_tokens = tooling.result_token_budget if tooling is not None else None
                result_budget = (
                    result_tokens * 4
                    if result_tokens is not None
                    else config.agent.tool_result_max_characters
                )
                item_limit = tooling.result_item_limit if tooling is not None else None
                artifact_store = (
                    self._service._tool_artifacts
                    if outcome.images or (tooling is not None and tooling.result_artifact_enabled)
                    else None
                )
                retention_seconds = (
                    tooling.result_artifact_retention_seconds if tooling is not None else None
                )
                from qq_ai_bot.runtime.work_activation import current_work_control
                from qq_ai_bot.tool_results.access import access_from_runtime

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
                    await self._service._record_tool_invocation(
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
            if is_web_tool:
                self._web_calls_used += 1
                self._web_was_used = True
        decoded = self._service._decode_tool_result(result)
        if self._is_mutating_call(call):
            if descriptor.provider_id != "admin" and not decoded.get("ok"):
                return result
            if bool(decoded.get("ok")):
                if mutation_identity is not None and mutation_committed:
                    self._completed_admin_mutations.add(mutation_identity)
                    self._mutation_committed = True
            elif not decoded.get("retryable") and (
                decoded.get("error") or decoded.get("error_code")
            ) not in _ADMIN_RETRYABLE_ERRORS | {
                "duplicate_mutation",
                "memory_candidate_ambiguous",
                "memory_candidate_not_found",
            }:
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
        from qq_ai_bot.capabilities.invocation import counts_toward_business_limit

        return counts_toward_business_limit(name)

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

    def _request_runtime(self) -> ToolRuntime:
        return self._runtime


def _refused_result(
    name: str, error_code: str, *, detail: str = "", restart_required: bool = False
) -> str:
    """Publish one pre-dispatch refusal as typed evidence and its model view."""
    from qq_ai_bot.runtime.effect_outcomes import current_result_capture

    outcome = ToolExecutionResult(
        ok=False,
        error_code=error_code,
        public_message=detail,
        mutation_committed=False,
        data={"executed": False, **({"restart_required": True} if restart_required else {})},
        provider_id="core",
        tool_name=name,
    )
    capture = current_result_capture.get()
    if capture is not None:
        capture.outcome = outcome
    return json.dumps(outcome.model_payload(), ensure_ascii=False)


_WORKER_TOOL_NOT_DECLARED = '{"ok":false,"error":"worker_tool_not_declared"}'


def _worker_tool_not_declared(name: str) -> str:
    """Child refusal keeps its original model shape and publishes typed evidence."""
    from qq_ai_bot.runtime.effect_outcomes import current_result_capture

    capture = current_result_capture.get()
    if capture is not None:
        capture.outcome = ToolExecutionResult(
            ok=False,
            error_code="worker_tool_not_declared",
            mutation_committed=False,
            data={"executed": False},
            provider_id="core",
            tool_name=name,
        )
    return _WORKER_TOOL_NOT_DECLARED
