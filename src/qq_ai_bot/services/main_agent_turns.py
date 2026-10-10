"""Shared preparation and execution boundary for composed Yuki turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass, replace
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.observations import ContextObservation
from qq_ai_bot.conversation.projections import (
    ProjectionCapacityError,
    PromptProjectionRepository,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse, InboundMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.execution_trace.phases import collect_phase_metrics
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_tasks import ActivationTasks
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.context_boundary import ContextBoundary, PreparedContextBoundary
from qq_ai_bot.services.durable_invocations import DurableInvocations
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.services.prompt_composer import PromptComposer, PromptComposition
from qq_ai_bot.services.turn_transcript import (
    DispatchOrigin,
    dispatch_request,
    validating_request,
)
from qq_ai_bot.vision.models import VisualObservation


def _same_request_input(left: object, right: object) -> bool:
    """Conservative type-sensitive equality for one preparation's estimate.

    Python considers True, 1 and 1.0 equal; their serialized token costs differ.
    Unknown opaque objects are re-estimated instead of trusting their __eq__.
    """
    if type(left) is not type(right):
        return False
    if is_dataclass(left) and not isinstance(left, type):
        return all(
            _same_request_input(getattr(left, item.name), getattr(right, item.name))
            for item in fields(left)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return len(left) == len(right) and all(
            _same_request_input(a, b) and _same_request_input(left[a], right[b])
            for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_request_input(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is float:
        return repr(left) == repr(right)  # JSON distinguishes 0.0 and -0.0.
    if left is None or type(left) in {str, int, bool, bytes}:
        return left == right
    if isinstance(left, Enum) and isinstance(right, Enum):
        return _same_request_input(left.value, right.value)
    return False


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

    async def recovery_contract(self, runtime: RuntimeConfigSnapshot) -> str | None:
        contract = self._runner.main_contract
        if contract is None:
            return None
        return self._runner.work_contract(
            runtime, self._composer.static_messages(), await contract.model_definitions()
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
        read_scope: str | None = None,
        allowed_capabilities: frozenset[str] = frozenset(),
        before_preparation: Callable[[], Awaitable[None]] | None = None,
    ) -> PromptComposition:
        with self.executions.track():
            active = current_work_control.get()
            if (
                active is not None
                and active.current is not None
                and not getattr(context, "recovery_protocol", False)
            ):
                from qq_ai_bot.runtime.work_context_note import publish_pending_note

                await publish_pending_note(active)
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
            )
            if getattr(context, "recovery_protocol", False):
                return composition
            if (
                self._projections is None
                or contract is None
                or (context.current_event_id is None and not context.projection_scope)
                or context.read_version is None
                or context.read_version.conversation_id is None
            ):
                return composition
            definitions = await contract.model_definitions()
            definitions, native_definitions = self._runner.prepare_request_tools(
                definitions,
                runtime_config=runtime,
                allowed_capabilities=allowed_capabilities,
            )
            version = context.read_version
            conversation_id = version.conversation_id
            assert conversation_id is not None
            repository = self._projections
            actor_id = (
                inbound.person_id or "actorless"
                if inbound is not None
                else str(
                    active.source.get("actor_person_id")
                    or ("self" if active.source.get("principal_kind") == "self" else "actorless")
                )
                if active is not None
                else "actorless"
            )
            if read_scope is None:
                saved_access = (
                    json.loads(active.current["checkpoint_json"])
                    .get("context_note", {})
                    .get("access", {})
                    if active is not None and active.current is not None
                    else {}
                )
                read_scope = (
                    str(active.source.get("read_scope") or saved_access.get("read_scope") or "")
                    if active is not None
                    else ""
                ) or json.dumps(
                    {"memory": [], "plugin_id": None, "delegation_id": None}, sort_keys=True
                )
            # Separate per-actor selected memory views. Actorless wakeups cannot inherit
            # the private dynamic context assembled for a preceding human turn.
            view_key = _hash(
                [
                    "main-history-v2",
                    version.conversation_id,
                    context.projection_scope
                    if context.projection_scope not in {"", "main", "self_initiative"}
                    else "main",
                    actor_id,
                    read_scope,
                    include_plugin_context,
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
            capacity_getter = getattr(self._runner._models, "capacity", None)
            capacity = (
                capacity_getter(self._runner._task)
                if callable(capacity_getter)
                else ModelCapacity()
            )
            input_budget = capacity.input_budget(
                runtime.context.window_tokens, output_tokens=runtime.llm.max_output_tokens
            )
            # Plan before dispatch, using the same complete-request estimator.
            # Keep the existing rollup margin and 4096-token reserve for native
            # declarations/public runtime state and subsequent response pairing.
            # Runner still checks its actual request, including those additions.
            planning_budget = max(
                1,
                int(
                    min(input_budget, runtime.context.compaction_window_tokens)
                    * runtime.context.compaction_trigger_ratio
                )
                - 4096,
            )
            # PromptCompiler emits system, optional rollup, raw history, current.
            # Verify the actual history slot before substituting frozen entries.
            history_end = len(composition.messages) - 1
            history_start = history_end - len(context.history_messages)
            if composition.messages[history_start:history_end] != context.history_messages:
                raise ValueError("compiled history slot does not match assembled history")
            compiled_prefix = composition.messages[:history_start]
            base_prefix = compiled_prefix[:-1] if context.rollup_text.strip() else compiled_prefix
            compiled_current = composition.messages[-1:]
            prepared_request = self._runner._capacity_request(
                ChatRequest(
                    messages=composition.messages,
                    model=runtime.llm.model or "fake",
                    temperature=runtime.llm.temperature,
                    max_output_tokens=runtime.llm.max_output_tokens,
                    thinking_enabled=runtime.llm.thinking_enabled,
                    tools=definitions,
                    tool_choice="auto" if definitions or native_definitions else None,
                    native_tools=native_definitions,
                )
            )
            fresh_tokens = estimate_request_tokens(prepared_request)
            fresh_request = deepcopy(prepared_request)
            collect_phase_metrics(estimate_count=1)
            # The maintenance target may choose an already prepared summary;
            # it cannot require a foreground model while the original fits.
            fixed_request = replace(
                prepared_request, messages=(*compiled_prefix, *compiled_current)
            )
            fixed_tokens = (
                fresh_tokens
                if _same_request_input(fixed_request, fresh_request)
                else estimate_request_tokens(fixed_request)
            )
            collect_phase_metrics(
                estimate_count=int(not _same_request_input(fixed_request, fresh_request))
            )
            maintenance_budget = max(planning_budget, fixed_tokens)
            estimated_request: ChatRequest | None = None
            estimated_tokens = 0

            def request_tokens(request: ChatRequest) -> int:
                nonlocal estimated_request, estimated_tokens
                if _same_request_input(request, fresh_request):
                    return fresh_tokens
                # One preparation-local complete request, including media,
                # native declarations, opaque state and settings. Keep a copy
                # so mutation of a schema/payload cannot reuse a stale estimate.
                if not _same_request_input(estimated_request, request):
                    estimated_tokens = estimate_request_tokens(request)
                    collect_phase_metrics(estimate_count=1)
                    estimated_request = deepcopy(request)
                return estimated_tokens

            def history_fits(history: tuple[ChatMessage, ...]) -> bool:
                # Reuse the already compiled system/rollup and current envelope.
                # Trying to compile an oversized old epoch first can hit the
                # compiler's character limit before the capacity rebase occurs.
                request = replace(
                    prepared_request,
                    messages=(*compiled_prefix, *history, *compiled_current),
                )
                return fresh_tokens > input_budget or request_tokens(request) <= input_budget

            def context_fits(candidate: AssembledContext) -> bool:
                request = replace(
                    prepared_request,
                    messages=(
                        *base_prefix,
                        *self._composer._conversation_history(candidate),
                        *compiled_current,
                    ),
                )
                return request_tokens(request) <= maintenance_budget

            def context_hard_fits(candidate: AssembledContext) -> bool:
                request = replace(
                    prepared_request,
                    messages=(
                        *base_prefix,
                        *self._composer._conversation_history(candidate),
                        *compiled_current,
                    ),
                )
                return request_tokens(request) <= input_budget

            preparation_requests = 0

            async def summarize_observations(
                observations: tuple[ContextObservation, ...],
            ) -> dict[str, Any]:
                from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
                from qq_ai_bot.model_runtime.models import ModelExecutionPriority
                from qq_ai_bot.persistence.event_repository import EventLedgerRepository
                from qq_ai_bot.runtime.work_repository import WorkConflict
                from qq_ai_bot.services.ordinary_compaction import summarize_records

                async def execute(candidate: ChatRequest) -> ChatResponse:
                    dispatched = False

                    async def validate() -> None:
                        nonlocal preparation_requests, dispatched
                        if before_preparation is not None:
                            await before_preparation()
                        if active is not None:
                            await active.validate()
                        if not await EventLedgerRepository(
                            repository.database
                        ).read_version_matches(version):
                            raise WorkConflict("observation_compaction_source_changed")
                        if not dispatched:
                            if preparation_requests + 1 >= runtime.agent.max_model_requests:
                                raise WorkConflict("model_request_budget")
                            if active is not None:
                                await active.reserve_request(auxiliary=True)
                            preparation_requests += 1
                            dispatched = True

                    with model_dispatch_guard(validate):
                        return await self._runner._models.execute(
                            self._runner._task,
                            candidate,
                            priority=ModelExecutionPriority.FOREGROUND,
                            canonical_conversation_id=version.conversation_id,
                        )

                summary = await summarize_records(
                    [(f"observation:{row.id}", row.payload_json) for row in observations],
                    main_request=prepared_request,
                    structured_mode=self._runner._models.structured_output_mode(self._runner._task),
                    summary_budget=capacity.input_budget(
                        runtime.context.window_tokens,
                        output_tokens=runtime.context.compaction_output_tokens,
                    ),
                    output_tokens=runtime.context.compaction_output_tokens,
                    prepare=self._runner._capacity_request,
                    execute=lambda candidate: self._runner._concurrency.run_llm(
                        conversation_id, lambda: execute(candidate)
                    ),
                )
                return {
                    "version": 1,
                    "facts": summary["facts"],
                    "unresolved": summary["pending"],
                    "next_steps": summary["next_steps"],
                }

            prepared = await prepare_history(
                self._projections,
                context,
                view_key=view_key,
                context_key=_hash(
                    [version.conversation_id, version.generation, actor_id, read_scope]
                ),
                contract_revision=contract_revision,
                history_fits=history_fits,
                context_fits=context_fits,
                context_hard_fits=context_hard_fits,
                summarize_observations=summarize_observations,
                actor_id=actor_id,
                read_scope=read_scope,
            )
            composition = self._composer.with_history(composition, context, prepared.context)
            work_wakeup = context.projection_scope == "main" and context.current_event_id is None
            fragments = (
                prepared.fragments
                if work_wakeup
                else prepared.fragments.append_current(
                    context.current_event_id, composition.messages[-1]
                )
            )
            projection_closed = False
            observation_watermark = max(
                version.starts_after_event_id,
                max(version.visible_event_ids, default=0),
                context.current_event_id or 0,
            )

            async def commit_projection() -> None:
                nonlocal projection_closed
                sequence = dispatch_request()
                if (
                    sequence is None
                    or sequence.origin is DispatchOrigin.WORK_RECOVERY
                    or projection_closed
                ):
                    return
                approved_initial = (
                    *composition.messages,
                    *(sequence.public_initial_suffix or ()),
                )
                if sequence.layout_public_initial is not None:
                    valid_layout = sequence.layout_public_initial == composition.messages
                    expected = (
                        *composition.messages[:-1],
                        *sequence.layout_host_initial,
                        composition.messages[-1],
                        *sequence.layout_current_inputs,
                    )
                    valid_layout = valid_layout and sequence.messages[: len(expected)] == expected
                else:
                    valid_layout = sequence.messages[: len(approved_initial)] == approved_initial
                if not valid_layout:
                    await prepared.repository.invalidate_view(view_key, reason="protocol_changed")
                    projection_closed = True
                    return
                # Shared history freezes only approved chat and observations.
                # Tool calls, opaque responses and working summaries belong to
                # the temporary execution tail even for ordinary chat.
                submitted = fragments.append_protocol(sequence.public_initial_suffix or ())
                try:
                    publication = await prepared.prepare_commit(
                        submitted,
                        current_snapshot=None if work_wakeup else composition.current_snapshot,
                    )
                    control = current_work_control.get()
                    work_session = control.session if control is not None else None
                    guard = work_session.source_guard if work_session is not None else None
                    original_guard = guard.snapshot() if guard is not None else None
                    original_revision = (
                        work_session.source_revision if work_session is not None else None
                    )
                    selected_guard = None
                    selected_revision = original_revision
                    if control is not None and guard is not None:
                        from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
                        from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

                        selected_guard = WorkSourceGuard.restore(guard.snapshot())
                        try:
                            valid = await selected_guard.check(
                                control, observation_sources=publication.observation_sources
                            )
                            if work_session is not None:
                                selected_revision = work_session.source_revision
                        finally:
                            if work_session is not None and original_revision is not None:
                                work_session.source_revision = original_revision
                        if not valid:
                            raise HistorySourceChangedError(guard.version)
                    snapshot = None

                    async def publish(writer: AsyncSession) -> None:
                        nonlocal snapshot
                        snapshot = await publication(writer)

                    def stage() -> None:
                        if guard is not None and selected_guard is not None:
                            guard.version = selected_guard.version
                            guard.fingerprint = selected_guard.fingerprint
                            guard.additional_events = selected_guard.additional_events
                            if work_session is not None and selected_revision is not None:
                                work_session.source_revision = selected_revision

                    def rollback() -> None:
                        if guard is not None and original_guard is not None:
                            restored = type(guard).restore(original_guard)
                            guard.version = restored.version
                            guard.fingerprint = restored.fingerprint
                            guard.additional_events = restored.additional_events
                        if work_session is not None and original_revision is not None:
                            work_session.source_revision = original_revision

                    def finalize() -> None:
                        nonlocal projection_closed
                        assert snapshot is not None
                        prepared.committed = snapshot
                        prepared.committed_input = list(submitted.items)
                        projection_closed = True

                    candidate = PreparedContextBoundary(publish, stage, rollback, finalize)
                    if (
                        work_session is not None
                        and control is not None
                        and control.current is not None
                    ):
                        # Source rows may be prepared already, but actual observed
                        # selection is published with the original dispatched journal.
                        work_session.dispatch_boundary = candidate
                    else:
                        stage()
                        try:
                            async with repository.database.immediate_session() as writer:
                                await publish(writer)
                        except BaseException:
                            rollback()
                            raise
                        finalize()
                except ProjectionCapacityError:
                    await prepared.repository.invalidate_view(view_key, reason="capacity")
                    projection_closed = True

            async def observation_boundary(known: frozenset[int]) -> ContextBoundary | None:
                # This reader runs only when the original loop already has a next
                # request to make. It neither wakes a Work nor refreshes its prefix.
                nonlocal observation_watermark
                ledger = EventLedgerRepository(repository.database)
                current_version, rows = await ledger.read_scope_delta(
                    version.scope, after_event_id=observation_watermark
                )
                if (
                    current_version.conversation_id,
                    current_version.generation,
                    current_version.starts_after_event_id,
                ) != (
                    version.conversation_id,
                    version.generation,
                    version.starts_after_event_id,
                ):
                    from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

                    raise HistorySourceChangedError(version)
                fresh = tuple(row for row in rows if row.id not in known)
                if not rows:
                    return None
                renderer = ChatEventPromptRenderer(
                    rows,
                    bot_display_name=context.history_bot_display_name,
                    timezone=context.history_timezone,
                    yuki_account_ids=context.history_yuki_account_ids,
                )
                # Individual event fragments preserve arrival order when directed
                # inputs and ordinary group observations interleave.
                delta = tuple(
                    (ids, message)
                    for row in fresh
                    for _, ids, message in renderer.main_agent_history((row,))
                )
                committed = False

                async def prepare_boundary() -> PreparedContextBoundary:
                    nonlocal committed, observation_watermark
                    from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

                    if not await ledger.read_version_matches(current_version):
                        raise HistorySourceChangedError(current_version)
                    previous = prepared.committed
                    if previous is None:
                        raise ValueError("chat boundary requires an initial dispatched projection")
                    old = FrozenFragments.load(previous.items())
                    selected = old.extend_history(delta, delta)
                    control = current_work_control.get()
                    guard = control.session.source_guard if control and control.session else None
                    selected_guard = None
                    original_revision = (
                        control.session.source_revision if control and control.session else None
                    )
                    selected_revision = original_revision
                    if control is not None and guard is not None:
                        from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard

                        # Prove the original sources before publishing new ones.
                        # A rejected candidate must not mutate its persisted guard.
                        selected_guard = WorkSourceGuard.restore(guard.snapshot())
                        try:
                            valid = await selected_guard.check(
                                control, event_ids=selected.event_ids
                            )
                            if control.session is not None:
                                selected_revision = control.session.source_revision
                        finally:
                            if control.session is not None and original_revision is not None:
                                control.session.source_revision = original_revision
                        if not valid:
                            raise HistorySourceChangedError(guard.version)
                    projection_publication = await repository.prepare_commit(
                        view_key=view_key,
                        conversation_id=conversation_id,
                        generation=current_version.generation,
                        expected_source_revision=current_version.prompt_source_revision,
                        starts_after_event_id=current_version.starts_after_event_id,
                        context_key=prepared.context_key,
                        contract_revision=prepared.contract_revision,
                        items=list(selected.items),
                        expected_epoch=previous.epoch_id,
                        expected_revision=previous.revision,
                        actor_id=actor_id,
                        read_scope=read_scope,
                        selected_summary_text=previous.selected_summary_text,
                        selected_summary_coverage=previous.selected_summary_coverage,
                        selected_summary_kind=previous.selected_summary_kind,
                        selected_summary_renderer=previous.selected_summary_renderer,
                    )
                    original_guard = guard.snapshot() if guard is not None else None
                    snapshot = None

                    async def publish(session: AsyncSession) -> None:
                        nonlocal snapshot
                        snapshot = await projection_publication(session)

                    def stage() -> None:
                        if guard is not None and selected_guard is not None:
                            guard.version = selected_guard.version
                            guard.fingerprint = selected_guard.fingerprint
                            guard.additional_events = selected_guard.additional_events
                            if (
                                control is not None
                                and control.session is not None
                                and selected_revision is not None
                            ):
                                control.session.source_revision = selected_revision

                    def rollback() -> None:
                        if guard is not None and original_guard is not None:
                            restored = type(guard).restore(original_guard)
                            guard.version = restored.version
                            guard.fingerprint = restored.fingerprint
                            guard.additional_events = restored.additional_events
                        if (
                            control is not None
                            and control.session is not None
                            and original_revision is not None
                        ):
                            control.session.source_revision = original_revision

                    def finalize() -> None:
                        nonlocal committed, observation_watermark
                        assert snapshot is not None
                        prepared.committed = snapshot
                        prepared.committed_input = list(selected.items)
                        observation_watermark = max(row.id for row in rows)
                        committed = True

                    return PreparedContextBoundary(publish, stage, rollback, finalize)

                async def commit_boundary() -> None:
                    if committed:
                        return
                    candidate = await prepare_boundary()
                    candidate.stage()
                    try:
                        async with repository.database.immediate_session() as writer:
                            await candidate.publication(writer)
                    except BaseException:
                        candidate.rollback()
                        raise
                    candidate.finalize()

                return ContextBoundary(delta, commit_boundary, prepare_boundary)

            return replace(
                composition,
                commit_projection=commit_projection,
                preparation_model_requests=preparation_requests if active is None else 0,
                observation_boundary=(
                    observation_boundary
                    if context.projection_scope in {"", "main", "self_initiative"}
                    else None
                ),
            )

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
                and self._invocations is not None
                and runtime.canonical_conversation_id
            ):
                previous = None
                if not self._composer._settings.runtime_work_enabled:
                    from qq_ai_bot.runtime.work_repository import WorkRepository
                    from qq_ai_bot.services.durable_invocations import invocation_boundary

                    previous = await WorkRepository(self._invocations.database).by_source(
                        f"invocation:{invocation_boundary(runtime)}"
                    )
                if self._composer._settings.runtime_work_enabled or (
                    previous is not None and previous["state"] == "completed"
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
        public_suffix = ()
        validate = runtime.before_model_request

        async def validate_prepared() -> None:
            if validate is None:
                return
            sequence = dispatch_request()
            if sequence is None:
                await validate()
                return
            with validating_request(replace(sequence, public_initial_suffix=public_suffix)):
                await validate()

        runtime = replace(runtime, before_model_request=validate_prepared)
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
