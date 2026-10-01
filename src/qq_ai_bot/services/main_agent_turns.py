"""Shared preparation and execution boundary for composed Yuki turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.projections import (
    ProjectionCapacityError,
    ProjectionConflict,
    PromptProjectionRepository,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_responses import (
    OpenAICompatibleResponsesProvider,
    OpenAIResponsesProvider,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.activation_tasks import ActivationTasks
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.durable_invocations import DurableInvocations
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.services.prompt_composer import PromptComposer, PromptComposition
from qq_ai_bot.services.turn_transcript import dispatch_request
from qq_ai_bot.vision.models import VisualObservation


class MainAgentTurnService:
    """Capture turn state before compilation; never refresh a submitted input."""

    def __init__(
        self,
        composer: PromptComposer,
        runner: AgentRunner,
        database: Database | None = None,
        *,
        executions: ActivationTasks | None = None,
    ) -> None:
        self._composer = composer
        self._runner = runner
        self.executions = executions if executions is not None else ActivationTasks()
        self._invocations = (
            DurableInvocations(database, self._run_prepared, self.executions) if database else None
        )
        self._projections = (
            PromptProjectionRepository(
                database,
                reclaim=True,
            )
            if database is not None
            else None
        )

    async def compose(
        self,
        *,
        inbound: InboundMessage | None,
        context: AssembledContext,
        runtime: RuntimeConfigSnapshot,
        visual_observation: VisualObservation | None,
        visual_failure: bool,
        scope_type: ScopeType | None = None,
        include_plugin_context: bool = True,
        memory_exclusive_write: bool = False,
    ) -> PromptComposition:
        with self.executions.track():
            contract = self._runner.main_contract
            state = await asyncio.to_thread(contract.state.snapshot) if contract else None
            composition = self._composer.compose(
                inbound=inbound,
                context=context,
                runtime=runtime,
                visual_observation=visual_observation,
                visual_failure=visual_failure,
                scope_type=scope_type,
                include_plugin_context=include_plugin_context,
                short_state=state,
                memory_exclusive_write=memory_exclusive_write,
            )
            if (
                self._projections is None
                or contract is None
                or (context.current_event_id is None and not context.projection_scope)
                or context.read_version is None
                or context.read_version.conversation_id is None
            ):
                return composition
            await contract.definitions()
            version = context.read_version
            # Separate per-actor selected memory views. Actorless wakeups cannot inherit
            # the private dynamic context assembled for a preceding human turn.
            view_key = _hash(
                [
                    "main-history-v1",
                    version.conversation_id,
                    context.projection_scope,
                    inbound.sender.user_id if inbound is not None else "actorless",
                    include_plugin_context,
                    memory_exclusive_write,
                ]
            )
            if context.current_message.images:
                # Inline image bytes are deliberately ephemeral. Retire the old
                # representation before dispatch instead of silently resuming it on
                # the next text turn as if this image request had never happened.
                repository = self._projections
                retired = False

                async def retire_image_projection() -> None:
                    nonlocal retired
                    if not retired:
                        await repository.invalidate_view(view_key, reason="protocol_changed")
                        retired = True

                return replace(composition, commit_projection=retire_image_projection)
            profile_revision = getattr(self._runner._models, "profile_revision", None)
            contract_revision = _hash(
                [
                    composition.metrics.stable_prefix_hash,
                    contract.revision,
                    self._runner._models.model_name(self._runner._task),
                    self._runner._models.protocol(self._runner._task).value,
                    profile_revision(self._runner._task)
                    if callable(profile_revision)
                    else "legacy",
                    asdict(runtime.llm),
                    asdict(runtime.web),
                ]
            )
            prepared = await prepare_history(
                self._projections,
                context,
                view_key=view_key,
                context_key=_hash([version.generation, context.rollup_text]),
                contract_revision=contract_revision,
                max_history_characters=max(
                    0,
                    runtime.context.window_tokens * 3 - len(composition.messages[-1].content or ""),
                ),
            )
            composition = self._composer.compose(
                inbound=inbound,
                context=prepared.context,
                runtime=runtime,
                visual_observation=visual_observation,
                visual_failure=visual_failure,
                scope_type=scope_type,
                include_plugin_context=include_plugin_context,
                short_state=state,
                memory_exclusive_write=memory_exclusive_write,
            )
            fragments = prepared.fragments.append_current(
                context.current_event_id, composition.messages[-1]
            )
            representation_retired = False

            async def commit_projection() -> None:
                nonlocal representation_retired
                sequence = dispatch_request()
                if sequence is None or representation_retired:
                    return
                if sequence.messages[: len(composition.messages)] != composition.messages:
                    await prepared.repository.invalidate_view(view_key, reason="protocol_changed")
                    representation_retired = True
                    return
                try:
                    submitted = fragments.append_protocol(
                        sequence.messages[len(composition.messages) :]
                    )
                    if sequence.continuation is not None:
                        if sequence.continuation.protocol != "responses":
                            # Signed native reasoning stays in the private Work journal.
                            # A new conversation turn establishes its own projection boundary.
                            raise ProjectionConflict("opaque native checkpoint requires a boundary")
                        provider = {
                            "deepseek": DeepSeekResponsesProvider,
                            "openai": OpenAIResponsesProvider,
                            "openai_compatible": OpenAICompatibleResponsesProvider,
                        }.get(sequence.continuation.provider)
                        if provider is None:
                            raise ProjectionConflict("unknown Responses replay provider")
                        continuation = provider._request_continuation(
                            ChatRequest(
                                messages=(),
                                model="",
                                continuation=sequence.continuation,
                                continuation_items=sequence.items,
                            )
                        )
                        if continuation is None:
                            raise ProjectionConflict("missing Responses replay")
                        submitted = submitted.append_responses(continuation)
                except ProjectionConflict:
                    # Hidden reasoning and opaque continuation stay turn-local. The
                    # next turn must not claim this discarded sequence's epoch.
                    await prepared.repository.invalidate_view(view_key, reason="protocol_changed")
                    representation_retired = True
                    return
                try:
                    await prepared.commit(submitted)
                except ProjectionCapacityError:
                    await prepared.repository.invalidate_view(view_key, reason="capacity")
                    representation_retired = True

            return replace(composition, commit_projection=commit_projection)

    async def run(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
    ) -> AgentRunResult:
        # The compiler places the current task after history. Capture that exact
        # message before the separate work-status input is appended below.
        with self.executions.track():
            if runtime.compaction_brief is None and messages:
                if messages[-1].role != "user":
                    raise ValueError("main_agent_current_task_message_required")
                runtime = replace(runtime, compaction_brief=messages[-1])
            control = runtime.work_control or current_work_control.get()
            if (
                control is None
                and self._composer._settings.runtime_work_enabled
                and self._invocations is not None
                and runtime.canonical_conversation_id
            ):
                return await self._invocations.run(messages, runtime, backend)
            return await self._run_prepared(
                messages, replace(runtime, work_control=control), backend
            )

    async def _run_prepared(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
    ) -> AgentRunResult:
        control = runtime.work_control
        if control is not None:
            control.current_message = messages[-1] if messages else None
            messages = (
                *messages,
                ChatMessage(
                    role="user",
                    content=(
                        "[运行状态资料，不增加任何权限] "
                        + json.dumps(
                            await control.runtime_state(),
                            ensure_ascii=False,
                        )
                    ),
                ),
            )
        return await self._runner.run(
            messages,
            replace(
                runtime,
                dynamic_context_prepared=True,
                work_control=control,
            ),
            backend,
        )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
