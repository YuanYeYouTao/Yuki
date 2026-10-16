"""Shared preparation and execution boundary for composed Yuki turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.ordinary_admission import (
    OrdinaryAdmissionConflict,
    OrdinaryAdmissionRepository,
)
from qq_ai_bot.conversation.projections import (
    ProjectionCapacityError,
    PromptProjectionRepository,
)
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_tasks import ActivationTasks
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.context_boundary import (
    ContextBoundary,
    OrdinaryContextInput,
    OrdinaryContextReader,
    PreparedContextBoundary,
)
from qq_ai_bot.services.context_compaction import PublicCompactContext
from qq_ai_bot.services.durable_invocations import DurableInvocations
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.services.prompt_composer import PromptComposer, PromptComposition
from qq_ai_bot.services.turn_transcript import (
    DispatchOrigin,
    dispatch_request,
)


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
        include_plugin_context: bool = True,
        allowed_capabilities: frozenset[str] = frozenset(),
        before_preparation: Callable[[], Awaitable[None]] | None = None,
        ordinary_inputs: OrdinaryContextReader | None = None,
    ) -> PromptComposition:
        with self.executions.track():
            contract = self._runner.main_contract
            state = await asyncio.to_thread(contract.state.snapshot) if contract else None

            def compose_context(candidate: AssembledContext) -> PromptComposition:
                return self._composer.compose(
                    inbound=inbound,
                    context=candidate,
                    runtime=runtime,
                    include_plugin_context=include_plugin_context,
                    short_state=state,
                )

            composition = compose_context(context)
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
            version = context.read_version
            conversation_id = version.conversation_id
            assert conversation_id is not None
            repository = self._projections
            ordinary_repository = OrdinaryAdmissionRepository(repository.database)
            ordinary_activation = (
                await ordinary_repository.current(
                    inbound.source_event_id,
                    conversation_id=conversation_id,
                    generation=version.generation,
                )
                if ordinary_inputs is not None
                and inbound is not None
                and inbound.source_event_id is not None
                else None
            )
            view_key = conversation_id
            prepared = await prepare_history(
                self._projections,
                context,
                view_key=view_key,
                context_key=_hash([version.conversation_id, version.generation]),
            )
            composition = compose_context(prepared.context)
            assert composition.read_version is not None
            ordinary_source_guard = WorkSourceGuard(composition.read_version)
            fragments = (
                prepared.fragments.append_current(context.current_event_id, context.current_message)
                if context.current_event_id is not None
                and context.current_event_id not in prepared.fragments.event_ids
                else prepared.fragments
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
                if sequence is None:
                    return
                control = current_work_control.get()
                if control is None:
                    from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

                    if not await ordinary_source_guard.check(None, database=repository.database):
                        raise HistorySourceChangedError(ordinary_source_guard.version)
                if sequence.origin is not DispatchOrigin.COMPOSED_INITIAL or projection_closed:
                    return
                approved_initial = composition.messages
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
                    projection_closed = True
                    return
                # Shared history freezes only admitted canonical chat events.
                # Tool calls, opaque responses and working summaries belong to
                # the temporary execution tail even for ordinary chat.
                submitted = fragments
                try:
                    publication = await prepared.prepare_commit(submitted)
                    control = current_work_control.get()
                    work_session = control.session if control is not None else None
                    guard = (
                        work_session.source_guard
                        if work_session is not None
                        else ordinary_source_guard
                    )
                    original_guard = guard.snapshot() if guard is not None else None
                    original_revision = (
                        work_session.source_revision if work_session is not None else None
                    )
                    selected_guard = None
                    selected_revision = original_revision
                    if guard is not None:
                        from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

                        selected_guard = WorkSourceGuard.restore(guard.snapshot())
                        try:
                            valid = await selected_guard.check(
                                control, database=repository.database
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
                selected_event_ids = frozenset(row.id for row in rows)
                ordinary_candidates: tuple[OrdinaryContextInput, ...] = ()
                control = current_work_control.get()
                if (
                    ordinary_inputs is not None
                    and ordinary_activation is not None
                    and ordinary_activation.route == "ordinary"
                    and (control is None or control.current is None)
                    and fresh
                ):
                    candidates = await ordinary_inputs(tuple(row.id for row in fresh))
                    # The original media preparation may persist a transcript or
                    # attachment result. Render that same source, outside the writer.
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
                    # Preparation may await external material. Refresh the same
                    # selection; later arrivals belong to the next boundary.
                    rows = tuple(row for row in rows if row.id in selected_event_ids)
                    fresh = tuple(row for row in rows if row.id not in known)
                    refreshed = {row.id: row for row in fresh}
                    ordinary_candidates = tuple(
                        candidate
                        for candidate in candidates
                        if candidate.prepared.event.id in refreshed
                        and candidate.prepared.admission.source_revision
                        == str(source_revision(refreshed[candidate.prepared.event.id]))
                        and candidate.prepared.event.segments
                        == refreshed[candidate.prepared.event.id].segments
                    )
                renderer = ChatEventPromptRenderer(
                    rows,
                    bot_display_name=context.history_bot_display_name,
                    timezone=context.history_timezone,
                    yuki_account_ids=context.history_yuki_account_ids,
                )
                # Individual event fragments preserve arrival order when directed
                # inputs and ordinary group observations interleave.
                delta = tuple(
                    (ids, replace(message, role="user"))
                    for row in fresh
                    for _, ids, message in renderer.main_agent_history((row,))
                )
                rendered_event_ids = {event_id for ids, _ in delta for event_id in ids}
                ordinary_candidates = tuple(
                    candidate
                    for candidate in ordinary_candidates
                    if candidate.prepared.event.id in rendered_event_ids
                )
                committed = False
                committed_inputs: list[OrdinaryContextInput] = []

                async def prepare_boundary(
                    business_inputs: bool = False,
                    inputs: tuple[OrdinaryContextInput, ...] = ordinary_candidates,
                ) -> PreparedContextBoundary:
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
                    if (
                        business_inputs
                        and inputs
                        and control is not None
                        and control.current is not None
                    ):
                        raise HistorySourceChangedError(current_version)
                    guard = (
                        control.session.source_guard
                        if control and control.session
                        else ordinary_source_guard
                    )
                    selected_guard = None
                    original_revision = (
                        control.session.source_revision if control and control.session else None
                    )
                    selected_revision = original_revision
                    if guard is not None:
                        # Prove the original sources before publishing new ones.
                        # A rejected candidate must not mutate its persisted guard.
                        selected_guard = WorkSourceGuard.restore(guard.snapshot())
                        try:
                            valid = await selected_guard.check(
                                control,
                                database=repository.database,
                                event_ids=selected.event_ids,
                            )
                            if control is not None and control.session is not None:
                                selected_revision = control.session.source_revision
                        finally:
                            if (
                                control is not None
                                and control.session is not None
                                and original_revision is not None
                            ):
                                control.session.source_revision = original_revision
                        if not valid:
                            raise HistorySourceChangedError(guard.version)
                    rebuild = (
                        selected_guard is not None
                        and await repository.invalidation_reason(view_key) == "source_changed"
                    )
                    already_published = selected == old and not rebuild
                    projection_publication = await repository.prepare_commit(
                        view_key=view_key,
                        conversation_id=conversation_id,
                        generation=current_version.generation,
                        expected_source_revision=current_version.prompt_source_revision,
                        starts_after_event_id=current_version.starts_after_event_id,
                        context_key=prepared.context_key,
                        items=list(selected.items),
                        expected_epoch=None if rebuild else previous.epoch_id,
                        expected_revision=0 if rebuild else previous.revision,
                        rebuild_reason="source_changed" if rebuild else None,
                        selected_summary_text=previous.selected_summary_text,
                        selected_summary_coverage=previous.selected_summary_coverage,
                        selected_summary_kind=previous.selected_summary_kind,
                        selected_summary_renderer=previous.selected_summary_renderer,
                        previous_snapshot=previous,
                        write=not already_published,
                    )
                    original_guard = guard.snapshot() if guard is not None else None
                    snapshot = None

                    async def publish(session: AsyncSession) -> None:
                        nonlocal snapshot
                        snapshot = await projection_publication(session)
                        committed_inputs.clear()
                        if business_inputs:
                            assert ordinary_activation is not None or not inputs
                            for candidate in inputs:
                                assert ordinary_activation is not None
                                try:
                                    inserted = await ordinary_repository.commit_to_activation(
                                        candidate.prepared,
                                        ordinary_activation,
                                        session=session,
                                        admission_allowed=candidate.admission_allowed,
                                    )
                                except OrdinaryAdmissionConflict as exc:
                                    raise HistorySourceChangedError(current_version) from exc
                                if inserted:
                                    committed_inputs.append(candidate)

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
                        observation_watermark = max(selected_event_ids)
                        committed = True

                    return PreparedContextBoundary(publish, stage, rollback, finalize)

                async def commit_boundary(
                    business_inputs: bool = False,
                    inputs: tuple[OrdinaryContextInput, ...] = ordinary_candidates,
                ) -> None:
                    if committed and not business_inputs:
                        return
                    candidate = await prepare_boundary(business_inputs, inputs)
                    candidate.stage()
                    try:
                        async with repository.database.immediate_session() as writer:
                            await candidate.publication(writer)
                    except BaseException:
                        candidate.rollback()
                        raise
                    candidate.finalize()
                    for accepted in committed_inputs:
                        if accepted.on_committed is not None:
                            await accepted.on_committed()
                    committed_inputs.clear()

                return ContextBoundary(
                    delta,
                    commit_boundary,
                    prepare_boundary,
                    ordinary_candidates,
                )

            async def adopt_compact(
                summary: str, coverage: int, retained: FrozenFragments
            ) -> tuple[ChatMessage, ...]:
                nonlocal composition, fragments, version, projection_closed
                version = replace(version, selected_summary_text=summary)
                prepared.context = replace(
                    prepared.context,
                    history_messages=retained.messages(),
                    checkpoint_text=summary,
                    prompt_effective_coverage=coverage,
                    metrics=replace(prepared.context.metrics, checkpoint_kind="compact"),
                    read_version=version,
                )
                prepared.fragments = retained
                prepared.committed = await repository.read(view_key)
                prepared.committed_input = (
                    prepared.committed.items() if prepared.committed is not None else None
                )
                prepared.reason = None
                composition = compose_context(prepared.context)
                fragments = (
                    retained.append_current(context.current_event_id, context.current_message)
                    if context.current_event_id is not None
                    and context.current_event_id not in retained.event_ids
                    else retained
                )
                projection_closed = False
                public_compaction.messages = composition.messages[:-1]
                public_compaction.fragments = retained
                public_compaction.version = version
                return composition.messages

            public_compaction = PublicCompactContext(
                composition.messages[:-1],
                len(self._composer.static_messages()),
                prepared.fragments,
                version,
                prepared.context_key,
                repository,
                adopt_compact,
                prepared.context.history_event_fragments,
            )
            return replace(
                composition,
                commit_projection=commit_projection,
                observation_boundary=observation_boundary,
                public_compaction=public_compaction,
            )

    async def read_result(self, runtime: AgentRuntime) -> AgentRunResult | None:
        control = runtime.work_control or current_work_control.get()
        if (
            control is not None
            or self._invocations is None
            or not runtime.canonical_conversation_id
        ):
            return None
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
        from qq_ai_bot.services.durable_invocations import invocation_boundary

        previous = await WorkRepository(self._invocations.database).by_source(
            f"invocation:{invocation_boundary(runtime)}"
        )
        if previous is None:
            return None
        async with self._invocations.database.sessions() as session:
            conversation = await session.get(
                CanonicalConversationModel, runtime.canonical_conversation_id
            )
            if conversation is None:
                raise WorkConflict("work_conversation_unavailable")
            generation = conversation.generation
        return await self._invocations.read_result(runtime, previous, generation)

    async def run(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
    ) -> AgentRunResult:
        with self.executions.track():
            if runtime.compact_focus is not None:
                return await self._run_prepared(
                    messages, replace(runtime, work_control=None), backend
                )
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
                if self._composer._settings.runtime_work_enabled or previous is not None:
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
        result = await self._runner.run(
            messages,
            replace(
                runtime,
                dynamic_context_prepared=True,
                work_control=control,
            ),
            backend,
        )
        return result


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
