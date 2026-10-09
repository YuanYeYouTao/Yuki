"""Compaction preserves the data it claims to have read, including source identity."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tests.unit.rollup_test_helpers import model_summary

from qq_ai_bot.conversation.rollup.models import RollupCandidate, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.renderer import (
    render_rollup_message,
    rollup_source_projection,
    serialize_compaction_source_events,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatResponse, ModelResponseStatus
from qq_ai_bot.model_runtime.models import StructuredOutputMode
from qq_ai_bot.persistence.repository_records import EventRecord


def event() -> EventRecord:
    return EventRecord(
        id=42,
        bot_user_id="9000",
        platform_message_id="transport-only",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="",
        visual_summary="",
        segments=({"type": "at", "data": {"qq": "9000"}},),
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        sender_group_card="same-name",
        group_id="group",
        mentioned_user_ids=("9000",),
        reply_to_event_id=12,
        author_kind="person",
        author_person_id="person-one",
    )


def test_source_preserves_internal_identity_reply_and_segment_only_mentions() -> None:
    source = rollup_source_projection(event())
    assert '"event_id":42' in source
    assert '"author_person_id":"person-one"' in source
    assert '"direction":"inbound"' in source
    assert '"reply_to_event_id":12' in source
    assert "提及:" in source
    assert "transport-only" not in source
    assert source != rollup_source_projection(replace(event(), author_person_id="person-two"))


def test_oversized_source_cannot_be_silently_truncated() -> None:
    from qq_ai_bot.conversation.rollup.prompt_accounting import source_accounting_characters
    from qq_ai_bot.conversation.rollup.repository import take_batch

    source_event = replace(event(), content="先不要发布。" * 30 + "必须等用户批准。", segments=())
    batch = take_batch((source_event,), RollupPolicyConfig(batch_max_characters=80))
    source = serialize_compaction_source_events(batch)
    assert batch == (source_event,)
    assert source.endswith("必须等用户批准。")
    assert source_accounting_characters(batch) == len(source) > 80


class RecordingModel:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.sources: list[str] = []
        self.fail_at = fail_at
        self.output_budgets: list[int | None] = []

    def structured_output_mode(self, _task):
        return StructuredOutputMode.TEXT_JSON

    async def execute(self, _task, request, *, priority=None, canonical_conversation_id=None):
        del priority
        self.output_budgets.append(request.max_output_tokens)
        body = request.messages[-1].content
        self.sources.append(body.split("New source events:\n", 1)[1])
        if self.fail_at == len(self.sources):
            raise RuntimeError("model disconnected")
        return ChatResponse(
            content=model_summary(request, "Continuity: pending approval."), latency_seconds=0
        )


def candidate() -> RollupCandidate:
    events = (replace(event(), content="new-source " * 100 + "LAST_CONSTRAINT", segments=()),)
    return RollupCandidate(
        "11111111-1111-4111-8111-111111111111", 1, 0, 0, "", events, 1, 100, "source-fingerprint"
    )


@pytest.mark.asyncio
async def test_model_reads_every_source_chunk_before_returning_semantic_result() -> None:
    model = RecordingModel()
    policy = RollupPolicyConfig(batch_max_characters=256)
    service = ConversationRollupService(models=model, config=policy, timeout_seconds=2)
    text, kind = await service.summarize_candidate(candidate())
    assert kind.value == "model"
    assert '"continuity":"Continuity' in text
    assert "".join(model.sources) == serialize_compaction_source_events(candidate().events)
    assert len(model.sources) > 1
    assert all(len(source) <= 256 for source in model.sources)


@pytest.mark.asyncio
async def test_failed_source_chunk_cannot_return_semantic_success() -> None:
    model = RecordingModel(fail_at=2)
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(batch_max_characters=256), timeout_seconds=2
    )
    with pytest.raises(RuntimeError, match="disconnected"):
        await service.summarize_candidate(candidate())
    assert service.metrics.model_summaries == 0


@pytest.mark.parametrize("payload_kind", ["valid", "truncated", "wrong_source"])
async def test_incomplete_provider_label_uses_actual_rollup_json_and_source(payload_kind):
    class IncompleteModel(RecordingModel):
        async def execute(self, task, request, **kwargs):
            response = await super().execute(task, request, **kwargs)
            content = response.content
            if payload_kind == "truncated":
                content = content[:-1]
            elif payload_kind == "wrong_source":
                value = json.loads(content)
                value["source_event_ids"] = [999999]
                content = json.dumps(value)
            return replace(
                response,
                content=content,
                status=ModelResponseStatus.INCOMPLETE,
                incomplete_reason="max_output_tokens",
            )

    model = IncompleteModel()
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(batch_max_characters=256), timeout_seconds=2
    )
    if payload_kind != "valid":
        reason = (
            "rollup_summary_invalid_json"
            if payload_kind == "truncated"
            else "rollup_summary_unsupplied_reference"
        )
        with pytest.raises(ValueError, match=reason):
            await service.summarize_candidate(candidate())
        assert service.metrics.model_summaries == 0
        assert len(model.sources) == 1
    else:
        text, kind = await service.summarize_candidate(candidate())
        assert kind.value == "model"
        assert json.loads(text)["source_event_ids"] == [42]
        assert "".join(model.sources) == serialize_compaction_source_events(candidate().events)
        assert service.metrics.model_summaries == 1


@pytest.mark.parametrize("mode", list(StructuredOutputMode))
@pytest.mark.parametrize("complete_json", [False, True])
async def test_structured_runner_decodes_incomplete_label_without_extra_model_calls(
    mode, complete_json
):
    from pydantic import BaseModel
    from tests.support.model_executor import InjectedModelExecutor

    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.model_runtime.models import ModelTask
    from qq_ai_bot.model_runtime.structured import StructuredTaskError, StructuredTaskRunner

    class Result(BaseModel):
        value: int

    content = '{"value":1}' if complete_json else '{"value":'
    response = ChatResponse(
        content=content if mode is not StructuredOutputMode.FUNCTION_TOOL else "",
        tool_calls=(ToolCall("result", ToolFunction("emit_result", content)),)
        if mode is StructuredOutputMode.FUNCTION_TOOL
        else (),
        latency_seconds=0,
        status=ModelResponseStatus.INCOMPLETE,
        incomplete_reason="max_output_tokens",
    )
    provider = FakeLLMProvider(lambda request: response)
    runner = StructuredTaskRunner(InjectedModelExecutor(provider))
    arguments = dict(
        task=ModelTask.CONVERSATION_COMPACTION,
        instruction="Return the actual value",
        structured_input={"value": 1},
        output_model=Result,
        mode=mode,
    )
    if complete_json:
        result, original = await runner.run_with_response(**arguments)
        assert result.value == 1
        assert original is response
    else:
        with pytest.raises(StructuredTaskError) as failed:
            await runner.run_with_response(**arguments)
        assert failed.value.reason_code == "json_decode"
        assert failed.value.response is response
    assert len(provider.requests) == 1


@pytest.mark.parametrize("complete_json", [False, True])
async def test_ordinary_compaction_uses_actual_json_under_incomplete_provider_label(complete_json):
    from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ToolCall, ToolFunction
    from qq_ai_bot.runtime.work_repository import WorkCapacityError
    from qq_ai_bot.services.ordinary_compaction import compact_ordinary
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    initial = (ChatMessage("system", "fixed contract"), ChatMessage("user", "current request"))
    transcript = TurnTranscript(initial)
    call = ToolCall("original-read", ToolFunction("read_probe", "{}"))
    transcript.append(ChatMessage("assistant", None, tool_calls=(call,)))
    transcript.append_result(call.id, '{"ok":true,"data":"original result"}')
    original = transcript.request()
    requests = []

    async def execute(request):
        requests.append(request)
        source = json.loads(request.messages[-1].content)
        content = json.dumps(
            {
                "facts": [{"text": "original read completed", "refs": source["source_refs"]}],
                "pending": [],
                "next_steps": [],
            }
        )
        return ChatResponse(
            content=content if complete_json else content[:-1],
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        )

    arguments = dict(
        main_request=ChatRequest(messages=original.messages),
        structured_mode=StructuredOutputMode.TEXT_JSON,
        summary_budget=128000,
        input_budget=128000,
        output_tokens=8192,
        prepare=lambda request: request,
        execute=execute,
        evidence=[{"effect_key": "original-read-key", "ok": True}],
    )
    if complete_json:
        candidate = await compact_ordinary(initial, transcript, **arguments)
        capsule = json.loads(candidate.request().messages[-1].content)
        assert capsule["summary"]["facts"][0]["text"] == "original read completed"
        assert capsule["execution_evidence"] == arguments["evidence"]
        assert candidate.request().messages[:2] == initial
    else:
        with pytest.raises(WorkCapacityError, match="ordinary_compaction_invalid_structure"):
            await compact_ordinary(initial, transcript, **arguments)
    assert transcript.request() == original
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_source_chunks_fit_actual_compaction_profile_input_budget() -> None:
    from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens

    class SmallInputModel(RecordingModel):
        def capacity(self, _task):
            return ModelCapacity(input_tokens=3000)

        async def execute(self, task, request, *, priority=None, canonical_conversation_id=None):
            assert estimate_request_tokens(request) <= 3000
            return await super().execute(task, request, priority=priority)

    model = SmallInputModel()
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(batch_max_characters=32_768), timeout_seconds=2
    )
    large = replace(
        candidate(), events=(replace(event(), content="new-source " * 2000, segments=()),)
    )
    await service.summarize_candidate(large)
    assert len(model.sources) > 1
    assert "".join(model.sources) == serialize_compaction_source_events(large.events)


def test_emergency_view_discloses_missing_history_and_internal_source_range() -> None:
    message = render_rollup_message("surviving tail", kind="emergency", covered_through_event_id=42)
    assert "Incomplete emergency" in message.content
    assert "not a complete semantic summary" in message.content
    assert "event_id=42" in message.content
    assert message.role == "user"


@pytest.mark.asyncio
async def test_candidate_carries_its_hot_output_policy() -> None:
    model = RecordingModel()
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(), timeout_seconds=2
    )
    policy = RollupPolicyConfig(max_output_tokens=1234, batch_max_characters=256)
    await service.summarize_candidate(replace(candidate(), policy=policy))
    assert model.output_budgets and set(model.output_budgets) == {1234}


async def _seed_private(database, *, peer: str, count: int, policy: RollupPolicyConfig):
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, "8000")
        await ensure_person(session, peer)
    scope = ConversationScope.private("8000", peer)
    writer = ScopedEventLedgerUnitOfWork(database, config=policy)
    for index in range(count):
        await writer.append(
            scope=scope,
            platform_message_id=f"{peer}-{index}",
            sender_user_id=peer,
            direction="inbound",
            content="tiny fact",
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    return scope


@pytest.mark.asyncio
async def test_activity_window_keeps_more_than_512_small_events_without_compaction(
    database,
) -> None:
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository

    policy = RollupPolicyConfig()
    scope = await _seed_private(database, peer="1010", count=520, policy=policy)
    repository = ConversationRollupRepository(database, policy)
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.raw_complete
    assert len(snapshot.raw_events) == 520
    assert await repository.claim_next_job(lease_owner="capacity", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_hot_scope_policy_isolated_and_incomplete_raw_prefix_is_explicit(database) -> None:
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository

    base = RollupPolicyConfig(context_token_budget=10_000)
    first = await _seed_private(database, peer="1011", count=5, policy=base)
    second = await _seed_private(database, peer="1012", count=5, policy=base)
    budgets = {"1011": 20, "1012": 10_000}

    async def policy_for_scope(scope):
        await asyncio.sleep(0)
        return replace(base, context_token_budget=budgets[scope.private_peer_user_id])

    repository = ConversationRollupRepository(database, base, policy_for_scope=policy_for_scope)
    a, b = await asyncio.gather(
        repository.load_prompt_snapshot(first), repository.load_prompt_snapshot(second)
    )
    assert not a.raw_complete and len(a.raw_events) < 5
    assert b.raw_complete and len(b.raw_events) == 5
    budgets["1011"] = 10_000
    reread = await repository.load_prompt_snapshot(first)
    assert reread.raw_complete and len(reread.raw_events) == 5
    assert repository.config == base


@pytest.mark.asyncio
async def test_durable_prerequisite_keeps_actual_request_budget_until_deadline(database) -> None:
    import json
    import time

    from sqlalchemy import update

    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.runtime.work_schema_v1 import work

    policy = RollupPolicyConfig(context_token_budget=10_000)
    scope = await _seed_private(database, peer="1013", count=5, policy=policy)
    repository = ConversationRollupRepository(database, policy)
    snapshot = await repository.load_prompt_snapshot(scope)
    owner = WorkRepository(database)
    lease = await owner.acquire(snapshot.scope.id, 1)
    item = await owner.accept(
        lease, source_key="capacity-prerequisite", source={}, goal="keep task"
    )
    try:
        checkpoint = {
            "context_rollup": {"coverage": 0, "token_budget": 60, "deadline": time.time() + 90}
        }
        async with database.immediate_session() as session:
            await session.execute(
                update(work)
                .where(work.c.id == item["id"])
                .values(checkpoint_json=json.dumps(checkpoint))
            )
        claim = await repository.claim_scope_for_foreground(
            scope, lease_owner="fit", lease_seconds=30
        )
        assert claim is not None
        candidate = await repository.candidate_for_claim(claim)
        assert candidate is not None and candidate.policy.context_token_budget == 60
        checkpoint["context_rollup"]["deadline"] = time.time() - 1
        async with database.immediate_session() as session:
            await session.execute(
                update(work)
                .where(work.c.id == item["id"])
                .values(checkpoint_json=json.dumps(checkpoint))
            )
        assert await repository.candidate_for_claim(claim) is None
    finally:
        await owner.release(lease)


@pytest.mark.asyncio
async def test_plugin_capacity_reads_hot_snapshot_and_shared_fixed_contract_reserve() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import make_settings

    from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
    from qq_ai_bot.persistence.event_repository import ConversationReadVersion
    from qq_ai_bot.services.context_assembler import ContextAssembler
    from qq_ai_bot.time.models import TimeContext

    scope = ConversationScope.private("8000", "1001")
    version = ConversationReadVersion(scope, "canonical", 1, 0, 0, (), (0, 0))
    ledger = MagicMock()
    ledger.read_scope_context = AsyncMock(return_value=(version, ()))
    assembler = ContextAssembler(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        people=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
        history_budget=lambda runtime: runtime.context.window_tokens - 2048,
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(window_tokens=8192, compaction_window_tokens=90000)
    )
    arguments = dict(
        inbound=InboundMessage(
            "one", "message", ScopeType.PRIVATE, SenderIdentity("1001"), "", bot_user_id="8000"
        ),
        content="x" * 25_000,
        metadata={},
        current_time=TimeContext(
            utc=datetime(2026, 10, 1, tzinfo=UTC),
            local=datetime(2026, 10, 1, tzinfo=UTC),
            timezone="UTC",
        ),
        read_history=False,
        projection_scope="plugin",
        runtime=runtime,
    )
    with pytest.raises(ConversationCoverageError, match="explicit compaction"):
        await assembler.assemble_plugin(**arguments)
    runtime.context.window_tokens = 16_384
    assert (await assembler.assemble_plugin(**arguments)).current_message.content == "x" * 25_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_kind", ["partial_metadata", "empty", "missing_text", "unknown_ref"]
)
async def test_ordinary_summary_defaults_keep_uncovered_original_pairs(payload_kind):
    from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ToolCall, ToolFunction
    from qq_ai_bot.runtime.work_repository import WorkCapacityError
    from qq_ai_bot.services.ordinary_compaction import compact_ordinary
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    initial = (ChatMessage("system", "fixed"), ChatMessage("user", "keep actual result"))
    transcript = TurnTranscript(initial)
    call = ToolCall("original", ToolFunction("read_probe", "{}"))
    transcript.append(ChatMessage("assistant", None, tool_calls=(call,)))
    transcript.append_result(call.id, '{"ok":true,"data":"MUST_KEEP_ACTUAL_RESULT"}')
    requests = []

    async def execute(request):
        requests.append(request)
        payload = {
            "partial_metadata": {
                "facts": [
                    {
                        "text": "partial summary",
                        "refs": ["record:0", "record:0"],
                        "annotation": "unused",
                    }
                ],
                "annotation": "unused",
            },
            "empty": {},
            "missing_text": {"facts": [{"refs": ["record:0"]}]},
            "unknown_ref": {"facts": [{"text": "invented source", "refs": ["record:999"]}]},
        }[payload_kind]
        return ChatResponse(json.dumps(payload), 0)

    arguments = dict(
        main_request=ChatRequest(messages=transcript.request().messages),
        structured_mode=StructuredOutputMode.TEXT_JSON,
        summary_budget=128000,
        input_budget=128000,
        output_tokens=8192,
        prepare=lambda request: request,
        execute=execute,
        evidence=[{"effect_key": "original-key", "ok": True}],
    )
    if payload_kind in {"missing_text", "unknown_ref"}:
        with pytest.raises(WorkCapacityError, match="ordinary_compaction_invalid_"):
            await compact_ordinary(initial, transcript, **arguments)
    else:
        compacted = await compact_ordinary(initial, transcript, **arguments)
        capsule = json.loads(compacted.request().messages[-1].content)
        assert capsule["summary"]["pending"] == capsule["summary"]["next_steps"] == []
        assert len(capsule["uncovered_records"]) == 2
        assert "MUST_KEEP_ACTUAL_RESULT" in json.dumps(capsule)
        assert capsule["execution_evidence"] == arguments["evidence"]
        if payload_kind == "empty":
            assert capsule["summary"]["facts"] == []
        else:
            assert capsule["summary"]["facts"][0]["refs"] == ["record:0"]
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_kind", ["partial_metadata", "uncited", "empty", "missing_text", "unknown_ref"]
)
async def test_rollup_format_defaults_preserve_raw_history_through_commit_and_reload(
    database, payload_kind
):
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository

    policy = RollupPolicyConfig(context_token_budget=60)
    scope = await _seed_private(database, peer="1014", count=5, policy=policy)
    repository = ConversationRollupRepository(database, policy)
    claim = await repository.claim_scope_for_foreground(
        scope, lease_owner="real-summary", lease_seconds=30
    )
    original = await repository.candidate_for_claim(claim)
    assert original is not None and len(original.events) > 1
    first = original.events[0].id

    class Model(RecordingModel):
        async def execute(self, *args, **kwargs):
            self.sources.append(args[1].messages[-1].content)
            payload = {
                "partial_metadata": {
                    "schema": "unused_provider_marker",
                    "continuity": "first tiny fact",
                    "source_event_ids": [first, first],
                    "open_issues": [
                        {
                            "text": "pending",
                            "source_event_ids": [first, first],
                            "metadata": "unused",
                        }
                    ],
                    "metadata": "unused",
                },
                "uncited": {"continuity": "partial narrative without citation fields"},
                "empty": {},
                "missing_text": {"source_event_ids": [first]},
                "unknown_ref": {"continuity": "invented source", "source_event_ids": [999999]},
            }[payload_kind]
            return ChatResponse(json.dumps(payload), 0)

    model = Model()
    service = ConversationRollupService(models=model, config=policy, timeout_seconds=2)
    if payload_kind in {"empty", "missing_text", "unknown_ref"}:
        expected = (
            "rollup_summary_unsupplied_reference"
            if payload_kind == "unknown_ref"
            else "rollup_summary_empty_continuity"
        )
        with pytest.raises(ValueError, match=expected):
            await service.summarize_candidate(original)
        assert (await repository.load_prompt_snapshot(scope)).rollup is None
        retried = await repository.candidate_for_claim(claim)
        assert (
            retried.events == original.events
            and retried.source_coverage == original.source_coverage
        )
    else:
        text, kind = await service.summarize_candidate(original)
        value = json.loads(text)
        assert value["schema"] == "conversation_rollup_v1" and value["corrections"] == []
        assert value["source_event_ids"] == [event.id for event in original.events]
        assert "Uncovered source records" in value["continuity"]
        retained = original.events[1:] if payload_kind == "partial_metadata" else original.events
        for source_event in retained:
            assert serialize_compaction_source_events((source_event,)) in value["continuity"]
        await repository.commit_candidate(claim, original, summary_text=text, summary_kind=kind)
        reloaded = await ConversationRollupRepository(database, policy).load_prompt_snapshot(scope)
        assert reloaded.rollup.summary_text == text
        assert reloaded.rollup.covered_through_event_id == original.events[-1].id
        prompt = render_rollup_message(
            reloaded.rollup.summary_text,
            kind="model",
            covered_through_event_id=reloaded.rollup.covered_through_event_id,
        )
        assert "Uncovered source records" in prompt.content
    assert len(model.sources) == 1
