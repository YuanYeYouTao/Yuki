"""Final review regressions for paid compact, checkpoint ownership and recovery."""

import pytest
from tests.support.canonical_identity import ensure_space
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projections import PromptProjectionRepository
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.conversations import ConversationScope
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


@pytest.mark.parametrize(("maximum_views", "byte_budget"), [(1, None), (128, 4)])
async def test_public_compact_checkpoint_survives_projection_cache_eviction(
    database, tmp_path, maximum_views, byte_budget
):
    await social_env(database, tmp_path)
    # Each empty [] projection uses two bytes: three exceed the four-byte budget.
    # Exercise count eviction and byte eviction independently.
    view_count = 3 if byte_budget is not None else 2 * maximum_views + 1
    scopes = [ConversationScope.group("80001", str(20001 + index)) for index in range(view_count)]
    writer = ScopedEventLedgerUnitOfWork(database)
    for index, scope in enumerate(scopes[1:], start=1):
        async with database.immediate_session() as session:
            await ensure_space(session, scope.group_id)
        await seed_scoped_event(
            writer,
            scope=scope,
            platform_message_id=f"cache-{index}",
            sender_user_id="10001",
            direction="inbound",
            content=f"other chat {index}",
        )
    projections = (
        PromptProjectionRepository(database)
        if maximum_views == 128
        else PromptProjectionRepository(database, maximum_views=maximum_views)
    )
    if byte_budget is not None:
        projections = PromptProjectionRepository(
            database,
            maximum_views=maximum_views,
            total_bytes=byte_budget,
        )
    ledger = EventLedgerRepository(database)
    owner = scopes[0]
    for index, scope in enumerate(scopes):
        version, events = await ledger.read_scope_context(scope, limit=100)
        publication = await projections.prepare_commit(
            view_key=version.conversation_id,
            conversation_id=version.conversation_id,
            generation=version.generation,
            expected_source_revision=version.prompt_source_revision,
            starts_after_event_id=version.starts_after_event_id,
            context_key="review-cache",
            fragments=FrozenFragments.load([]),
            rebuild_reason="compact" if index == 0 else "bootstrap",
            selected_summary_text="Paid public checkpoint" if index == 0 else None,
            selected_summary_coverage=max(event.id for event in events) if index == 0 else 0,
            selected_summary_kind="compact" if index == 0 else None,
            selected_summary_renderer=2 if index == 0 else None,
        )
        async with database.immediate_session() as session:
            await publication(session)
        snapshot = await ConversationScopeRepository(database).load_prompt_snapshot(owner)
    owner_version, owner_events = await ledger.read_scope_context(owner, limit=100)
    assert await projections.invalidation_reason(owner_version.conversation_id) == "capacity"
    assert snapshot.checkpoint_text == "Paid public checkpoint"
    assert snapshot.effective_coverage == max(event.id for event in owner_events)
    assert not snapshot.raw_events


@pytest.mark.parametrize("protocol", ["gemini", "openai_responses", "deepseek_responses"])
async def test_native_compact_preserves_fitting_unobserved_tool_tail(
    protocol, database, tmp_path, monkeypatch
):
    import json

    import httpx

    from yuki.domain.messages import ChatMessage, ChatRequest, ChatTool
    from yuki.llm.deepseek_responses import DeepSeekResponsesProvider
    from yuki.llm.gemini import GeminiProvider
    from yuki.llm.openai_responses import OpenAIResponsesProvider
    from yuki.model_runtime.capacity import ModelCapacity, estimate_request_tokens
    from yuki.services.context_compaction import (
        EXECUTION_COMPACT_INSTRUCTION,
        execution_compact_plan,
    )
    from yuki.services.turn_transcript import TurnTranscript

    initial = (
        ChatMessage("system", "original static contract"),
        ChatMessage("user", "continue original research"),
    )
    transcript = TurnTranscript(initial)
    chat = TurnTranscript(initial)
    tools = (ChatTool("read_probe", "Read existing evidence", {"type": "object"}),)
    counter = 0
    wire = []
    responses = []

    def transport(request):
        nonlocal counter
        counter += 1
        wire.append(json.loads(request.content))
        if protocol != "gemini":
            return httpx.Response(
                200,
                json={
                    "id": f"response-{counter}",
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": f"thought-{counter}",
                            "encrypted_content": f"original-signature-{counter}",
                            "summary": [],
                        },
                        {
                            "type": "function_call",
                            "id": f"item-{counter}",
                            "call_id": f"call-{counter}",
                            "name": "read_probe",
                            "arguments": "{}",
                        },
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {"name": "read_probe", "args": {}},
                                    "thoughtSignature": f"original-signature-{counter}",
                                }
                            ],
                        },
                    }
                ]
            },
        )

    def request_for(current):
        sequence = current.request()
        return ChatRequest(
            messages=sequence.messages,
            continuation=sequence.continuation,
            continuation_items=sequence.items,
            model="gemini-test",
            tools=tools,
            max_output_tokens=1024,
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider_type = {
            "gemini": GeminiProvider,
            "openai_responses": OpenAIResponsesProvider,
            "deepseek_responses": DeepSeekResponsesProvider,
        }[protocol]
        provider = provider_type(
            base_url="https://wire.invalid/v1/",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        prior = "previous fact " * 1800
        fresh = "new evidence " * 1400
        for result in (prior, fresh):
            response = await provider.complete(request_for(transcript))
            assert response.continuation is not None
            transcript.accept(response.continuation)
            call_message = ChatMessage(
                "assistant", response.content, tool_calls=response.tool_calls
            )
            responses.append(call_message)
            transcript.append_result(response.tool_calls[0].id, result)
            chat.append(call_message)
            chat.append_result(response.tool_calls[0].id, result)
            if result == prior:
                fitting = transcript.prefix(len(transcript.entries()))
                fitting.append(ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION))
                budget = estimate_request_tokens(request_for(fitting)) + 1500

        main = request_for(transcript)
        assert estimate_request_tokens(main) > budget
        chat_plan = execution_compact_plan(
            chat, initial, request_for(chat), input_budget=budget, prepare=lambda request: request
        )
        assert any(message.content == fresh for message in chat_plan.retained)
        print(
            {
                "input_budget": budget,
                "full_gemini_tokens": estimate_request_tokens(main),
                "fitting_paired_prefix_tokens": estimate_request_tokens(request_for(fitting)),
                "fresh_tool_result_tokens": estimate_request_tokens(
                    ChatRequest(messages=(ChatMessage("user", fresh),))
                ),
                "physical_mock_requests": len(wire),
                "chat_tail_retained": len(chat_plan.retained),
            }
        )
        plan = execution_compact_plan(
            transcript,
            initial,
            main,
            input_budget=budget,
            prepare=lambda request: request,
            responses=tuple(responses),
        )
        assert plan.retained
        evidence = json.loads(plan.retained[-1].content)
        assert evidence["calls"][0]["result"] == fresh
        assert evidence["calls"][0]["call_id"] == response.tool_calls[0].id
        assert plan.request.continuation is not None
        maintenance_wire = provider._build_payload(plan.request)
        assert "original-signature-1" in json.dumps(maintenance_wire)
        assert "original-signature-2" not in json.dumps(maintenance_wire)
        fresh_wire = provider._build_payload(
            ChatRequest(messages=(*initial, *plan.retained), tools=tools, model="gemini-test")
        )
        assert fresh in json.dumps(fresh_wire)
        assert "thoughtSignature" not in json.dumps(fresh_wire)
        assert "original-signature-" not in json.dumps(fresh_wire)
        if protocol == "gemini":
            assert all(
                "functionCall" not in part
                for content in fresh_wire["contents"]
                for part in content["parts"]
            )
        else:
            assert all(item.get("type") != "function_call" for item in fresh_wire["input"])
        assert transcript.request().continuation == main.continuation

        # Exercise the real Main/session path, whose native transcript has no
        # Chat assistant tool message: call facts live in model_observations.
        from dataclasses import asdict, replace

        from tests.support.work_compaction_capacity_helpers import _runtime, _session

        from yuki.llm.fake import FakeLLMProvider
        from yuki.services.turn_execution import TurnExecution
        from yuki.web.models import WebMode

        control, session, _ = await _session(database, tmp_path)
        session.transcript = transcript
        session.sequence = len(responses)
        original_effect_key = session.call_key(responses[-1].tool_calls[0].id)
        session.progress["model_observations"] = [
            {
                "content": response.content,
                "tool_calls": [asdict(call) for call in response.tool_calls],
            }
            for response in responses
        ]
        summarizer = FakeLLMProvider(
            lambda request: "Earlier finding retained; continue original work."
        )
        runner, runtime = await _runtime(database, control, initial, summarizer)
        monkeypatch.setattr(
            runner._models, "capacity", lambda task: ModelCapacity(input_tokens=budget + 256)
        )
        runtime = replace(
            runtime,
            max_model_requests=None,
            auxiliary_requests=[0],
            runtime_config=replace(
                runtime.runtime_config,
                llm=replace(runtime.runtime_config.llm, max_output_tokens=256),
            ),
        )
        execution = TurnExecution(runner, initial, runtime, None)
        execution.state.transcript = transcript
        prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
        assert prepared.request.continuation is None
        assert fresh in "\n".join(message.content or "" for message in prepared.request.messages)
        assert "original-signature-" not in str(prepared.request.messages)
        assert original_effect_key in str(prepared.request.messages)


async def test_irreducible_raw_tail_does_not_buy_repeated_compacts(database, tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.support.work_compaction_capacity_helpers import _runtime, _session

    from yuki.domain.messages import ChatMessage, ToolCall, ToolFunction
    from yuki.llm.fake import FakeLLMProvider
    from yuki.model_runtime.capacity import ModelCapacity
    from yuki.runtime.work_repository import WorkCapacityError
    from yuki.services.turn_execution import TurnExecution
    from yuki.web.models import WebMode

    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "earlier completed finding " * 100))
    call = ToolCall("latest-unobserved-result", ToolFunction("read_probe", "{}"))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    session.transcript.append_result(call.id, "unread result " * 2000)
    provider = FakeLLMProvider(
        lambda request: "Earlier finding retained. Continue the original work."
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    monkeypatch.setattr(runner._models, "capacity", lambda task: ModelCapacity(input_tokens=4096))
    runtime = replace(
        runtime,
        max_model_requests=3,
        auxiliary_requests=[0],
        runtime_config=replace(
            runtime.runtime_config,
            llm=replace(runtime.runtime_config.llm, max_output_tokens=256),
        ),
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    with pytest.raises(WorkCapacityError, match="model_request_capacity"):
        await execution.prepare_request(0, (), WebMode.DISABLED)
    print(
        {
            "irreducible_tail_compact_calls": len(provider.requests),
            "capacity_budget": 4096,
            "segment_limit": 3,
            "retained_result_present_each_time": all(
                "unread result " in str(execution.state.transcript.request().messages) for _ in [0]
            ),
        }
    )
    assert not provider.requests, (
        "The retained input is already known not to fit before any paid compact"
    )


def test_0119_keeps_valid_rollup_checkpoint_without_cached_projection(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from alembic.config import Config
    from tests.unit.test_migration_0119 import _seed_0118

    path = tmp_path / "missing-projection-upgrade.sqlite3"
    _, identities = _seed_0118(path, monkeypatch)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM prompt_projections WHERE conversation_id=?", (identities[0],)
        )
        before = connection.execute(
            "SELECT summary_text,covered_through_event_id FROM canonical_conversation_rollups "
            "WHERE conversation_id=?",
            (identities[0],),
        ).fetchone()
    command.upgrade(Config("alembic.ini"), "head")
    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT selected_summary_text,selected_summary_coverage FROM prompt_projections "
            "WHERE conversation_id=?",
            (identities[0],),
        ).fetchone()
        print(
            {
                "valid_legacy_checkpoint": before,
                "migrated_checkpoint": after,
                "remaining_raw_events": connection.execute(
                    "SELECT count(*) FROM chat_events"
                ).fetchone()[0],
                "foreign_key_errors": list(connection.execute("PRAGMA foreign_key_check")),
            }
        )
    assert after == before


def test_public_plan_keeps_the_largest_fitting_complete_prefix():
    from dataclasses import replace
    from types import SimpleNamespace

    from yuki.domain.messages import ChatMessage, ChatRequest
    from yuki.model_runtime.capacity import estimate_request_tokens
    from yuki.services.context_compaction import (
        PUBLIC_COMPACT_INSTRUCTION,
        PublicCompactContext,
        public_compact_plan,
    )
    from yuki.services.turn_transcript import TurnTranscript

    fragments = FrozenFragments.load(
        [
            {
                "kind": "model_input",
                "event_ids": [i + 1],
                "message": {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": "ordinary chat " * 16,
                },
            }
            for i in range(8)
        ]
    )
    static = ChatMessage("system", "original static instructions")
    instruction = ChatMessage("user", PUBLIC_COMPACT_INSTRUCTION)
    messages = fragments.messages()
    version = SimpleNamespace(
        conversation_id="original", generation=1, prompt_source_revision=0, starts_after_event_id=0
    )

    async def adopt(*args):
        raise AssertionError("Planning does not publish")

    context = PublicCompactContext(
        (static, *messages), 1, fragments, version, "original", None, adopt
    )
    initial = (*context.messages, ChatMessage("user", "current trigger"))
    request = ChatRequest(messages=initial)
    budget = estimate_request_tokens(
        replace(request, messages=(static, *messages[:4], instruction))
    )
    plan = public_compact_plan(
        context,
        TurnTranscript(initial),
        request,
        initial=initial,
        input_budget=budget,
        prepare=lambda value: value,
    )
    coverage = plan.public["coverage"]
    assert 0 < coverage < len(messages)
    assert plan.request.messages == (static, *messages[:coverage], instruction)
    assert estimate_request_tokens(plan.request) <= budget
    assert (
        estimate_request_tokens(
            replace(plan.request, messages=(static, *messages[: coverage + 1], instruction))
        )
        > budget
    )
    assert FrozenFragments.load(plan.public["items"]).messages() == messages[coverage:]


async def test_status_does_not_report_source_invalidated_checkpoint(database, tmp_path):
    from sqlalchemy import update
    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.commands_and_chat_helpers import inbound

    from yuki.conversation.projections import PromptProjectionRepository
    from yuki.conversation.scope_repository import ConversationScopeRepository
    from yuki.domain.conversations import ConversationScope
    from yuki.llm.fake import FakeLLMProvider
    from yuki.persistence.event_repository import EventLedgerRepository
    from yuki.persistence.models import ChatEventModel

    harness = build_harness(
        database, make_settings(database.url), FakeLLMProvider(lambda request: "not called")
    )
    scope = ConversationScope.group("9999", "2001")
    await seed_scoped_event(
        harness.scoped_events,
        scope=scope,
        platform_message_id="source-for-status",
        sender_user_id="9000",
        direction="inbound",
        content="original public source",
    )
    version, events = await EventLedgerRepository(database).read_scope_context(scope, limit=100)
    coverage = max(event.id for event in events)
    repository = PromptProjectionRepository(database)
    publication = await repository.prepare_commit(
        view_key=version.conversation_id,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        context_key="status-review",
        fragments=FrozenFragments.load([]),
        rebuild_reason="compact",
        selected_summary_text="checkpoint using original public source",
        selected_summary_coverage=coverage,
        selected_summary_kind="compact",
        selected_summary_renderer=2,
    )
    async with database.immediate_session() as session:
        await publication(session)
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == coverage)
            .values(content="source corrected after checkpoint")
        )
    effective = await ConversationScopeRepository(database).load_prompt_snapshot(scope)
    assert effective.checkpoint_text == ""
    sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai status", message_id="status-after-source-edit", user_id="9000", group_id="2001"
        ),
        sender,
    )
    line = next(line for line in sender.messages[0].text.splitlines() if line.startswith("Compact"))
    print({"status_compact_line": line, "actual_effective_coverage": effective.effective_coverage})
    assert line == "Compact 检查点：无"


@pytest.mark.parametrize("same_coverage", [False, True])
async def test_paid_public_recovery_keeps_newer_checkpoint_after_cache_eviction(
    database,
    tmp_path,
    same_coverage,
):
    from dataclasses import replace

    from tests.support.work_compaction_capacity_helpers import _session
    from tests.unit.test_current_context_compaction import _append_public, _public_context

    from yuki.conversation.frozen_fragments import FrozenFragments
    from yuki.services.context_compaction import (
        PublicCompactContext,
        current_public_compact,
        public_compact_publication,
        render_context_checkpoint,
    )

    control, _, initial = await _session(database, tmp_path)
    original = await _public_context(database, control, initial)
    old_coverage = max(original.fragments.event_ids)
    paid = {
        "conversation_id": original.version.conversation_id,
        "generation": original.version.generation,
        "source_revision": original.version.prompt_source_revision,
        "starts_after_event_id": original.version.starts_after_event_id,
        "context_key": original.context_key,
        "coverage": old_coverage,
        "items": [],
    }
    if not same_coverage:
        await _append_public(database, "newer public correction")
    scope = ConversationScope.group("80001", "20001")
    version, events = await EventLedgerRepository(database).read_scope_context(scope, limit=100)
    latest_coverage = max(event.id for event in events)
    repository = PromptProjectionRepository(database, maximum_views=1)
    publication = await repository.prepare_commit(
        view_key=version.conversation_id,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        context_key=original.context_key,
        fragments=FrozenFragments.load([]),
        rebuild_reason="compact",
        selected_summary_text="Later adopted checkpoint",
        selected_summary_coverage=latest_coverage,
        selected_summary_kind="compact",
        selected_summary_renderer=2,
    )
    async with database.immediate_session() as writer:
        await publication(writer)
    async with database.immediate_session() as writer:
        await ensure_space(writer, "20002")
    await seed_scoped_event(
        ScopedEventLedgerUnitOfWork(database),
        scope=ConversationScope.group("80001", "20002"),
        platform_message_id="eviction-other",
        sender_user_id="10001",
        direction="inbound",
        content="other scope",
    )
    other_version, _ = await EventLedgerRepository(database).read_scope_context(
        ConversationScope.group("80001", "20002"), limit=100
    )
    eviction = await repository.prepare_commit(
        view_key=other_version.conversation_id,
        conversation_id=other_version.conversation_id,
        generation=other_version.generation,
        expected_source_revision=other_version.prompt_source_revision,
        starts_after_event_id=other_version.starts_after_event_id,
        context_key="other",
        fragments=FrozenFragments.load([]),
        rebuild_reason="bootstrap",
    )
    async with database.immediate_session() as writer:
        await eviction(writer)
    snapshot = await ConversationScopeRepository(database).load_prompt_snapshot(scope)
    assert snapshot.checkpoint_text == "Later adopted checkpoint"
    assert snapshot.effective_coverage == latest_coverage
    assert not snapshot.raw_events
    context = PublicCompactContext(
        (initial[0], render_context_checkpoint(snapshot.checkpoint_text, latest_coverage)),
        1,
        FrozenFragments.load([]),
        replace(version, selected_summary_text=snapshot.checkpoint_text),
        original.context_key,
        repository,
        original.adopt,
    )
    # Also fence a stale publisher that raced a later adoption. Cache availability
    # must not let this already-paid older checkpoint move coverage backwards.
    from yuki.conversation.projections import ProjectionConflict

    stale_publication = await public_compact_publication(repository, paid, "Older paid checkpoint")
    with pytest.raises(ProjectionConflict, match="newer checkpoint"):
        async with database.immediate_session() as writer:
            await stale_publication(writer)
    recovered, summary = await current_public_compact(context, paid, "Older paid checkpoint")
    recovered_publication = await public_compact_publication(repository, recovered, summary)
    async with database.immediate_session() as writer:
        await recovered_publication(writer)
    after = await ConversationScopeRepository(database).load_prompt_snapshot(scope)
    print(
        {
            "checkpoint_before_cache_eviction": "Later adopted checkpoint",
            "latest_coverage": latest_coverage,
            "old_paid_coverage": old_coverage,
            "checkpoint_after_old_paid_recovery": after.checkpoint_text,
            "coverage_after": after.effective_coverage,
            "recovered_items": recovered["items"],
            "raw_events_after_rollback": [event.id for event in after.raw_events],
        }
    )
    assert after.effective_coverage == latest_coverage
    assert after.checkpoint_text == "Later adopted checkpoint"
    assert not after.raw_events


async def test_two_unselected_source_changes_can_reprepare_same_journal(
    database, tmp_path, monkeypatch
):
    from dataclasses import replace

    from sqlalchemy import update
    from tests.support.work_compaction_capacity_helpers import _session
    from tests.unit.test_current_context_compaction import _append_public

    from yuki.domain.messages import ChatMessage
    from yuki.persistence.models import ChatEventModel
    from yuki.runtime.work_repository import WorkConflict
    from yuki.runtime.work_source_guard import WorkSourceGuard

    control, session, _ = await _session(database, tmp_path)
    scope = ConversationScope.group("80001", "20001")
    version, selected = await EventLedgerRepository(database).read_scope_context(scope, limit=100)
    await _append_public(database, "unselected newer event")
    fresh_version, all_events = await EventLedgerRepository(database).read_scope_context(
        scope, limit=100
    )
    unselected = max(event.id for event in all_events)
    version = replace(
        fresh_version,
        visible_event_ids=tuple(event.id for event in selected),
        selected_summary_text="",
    )
    guard = WorkSourceGuard(version)
    session.source_guard = guard
    assert await guard.check(control)
    assert unselected not in version.visible_event_ids
    session.transcript.append(
        ChatMessage("assistant", "original response held by the same activation")
    )
    original_save = session.journal.save
    attempts = 0

    async def change_unselected_then_save(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == unselected)
                    .values(content=f"newer event enrichment {attempts}")
                )
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(session.journal, "save", change_unselected_then_save)
    failure = None
    try:
        await session.save("response")
    except WorkConflict as error:
        failure = error.code
    sources_still_valid = await guard.check(control)
    print(
        {
            "journal_publication_attempts": attempts,
            "error": failure,
            "selected_sources_still_valid": sources_still_valid,
            "model_replayed": False,
            "changed_event_in_original_selection": unselected in version.visible_event_ids,
        }
    )
    assert failure is None, "An empirical one-recheck cap aborts a still valid original journal"


async def test_source_recheck_does_not_republish_after_unconfirmed_rollback(
    database,
    tmp_path,
    monkeypatch,
):
    from tests.support.work_compaction_capacity_helpers import _session

    from yuki.domain.messages import ChatMessage
    from yuki.runtime.work_repository import WorkConflict
    from yuki.runtime.work_source_guard import WorkSourceGuard

    control, session, _ = await _session(database, tmp_path)
    version, _ = await EventLedgerRepository(database).read_scope_context(
        ConversationScope.group("80001", "20001"),
        limit=100,
    )
    session.source_guard = WorkSourceGuard(version)
    assert await session.source_guard.check(control)
    session.transcript.append(ChatMessage("assistant", "original paid response"))
    attempts = []
    error = WorkConflict("work_journal_source_changed")
    error.add_note("rollback_failed:OperationalError")

    async def unknown_cleanup(*args, **kwargs):
        attempts.append(args)
        raise error

    monkeypatch.setattr(session.journal, "save", unknown_cleanup)
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await session.save("response")
    assert len(attempts) == 1  # No second publication while the original outcome is unknown.
