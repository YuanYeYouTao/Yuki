"""Capacity replacement preserves original input and paired execution facts."""

import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

# P10: fixed typed backend fixture, original assertions retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env
from tests.support.work_compaction import session_summary, summary_json

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ResponseCitation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelExecutionPriority, StructuredOutputMode
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


async def _session(database, tmp_path, *, worker=False):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "capacity-test", {"trigger_event_id": 1}, validate)
    control.current = await repository.accept(
        lease, source_key="capacity-test", source=control.source, goal="prepare an artifact"
    )
    if worker:
        from qq_ai_bot.runtime.subagent_repository import SubagentRepository

        children = SubagentRepository(repository)
        identity = await children.start(
            lease,
            control.current["id"],
            "capacity-child",
            {"goal": "prepare an artifact", "output_kind": "artifact"},
        )
        await repository.release(lease)
        child_lease = await children.acquire(identity)
        assert child_lease is not None

        async def validate_child():
            assert await repository.valid(child_lease)

        current = await repository.get(identity)
        control = WorkControl(
            repository,
            child_lease,
            current["source_key"],
            json.loads(current["source_json"]),
            validate_child,
        )
        control.current = current
    task = ChatMessage("user", "Prepare the artifact and preserve the original instructions.")
    initial = (ChatMessage("system", "fixed contract"), task)
    session = WorkSession(control, "capacity-contract")
    control.session = session
    await session.restore(TurnTranscript(initial), compaction_brief=task)
    return control, session, initial


async def _snapshot(database, work_id):
    async with database.sessions() as reader:
        return dict(
            (await reader.execute(select(journal).where(journal.c.work_id == work_id)))
            .mappings()
            .one()
        )


def _grow(transcript):
    # Old, replaceable history must sit outside the retained recent raw suffix.
    for _ in range(20):
        transcript.append(ChatMessage("assistant", "Completed investigation. " * 500))
    for _ in range(16):
        transcript.append(ChatMessage("assistant", "Recent completed check."))


async def _runtime(database, control, initial, provider, *, contract_workspace=None, **settings):
    harness = build_harness(database, make_settings(database.url, **settings), provider)
    chat = harness.processor._chat
    if contract_workspace is not None:
        chat.runtime.runner.main_contract = MainAgentContract(
            chat, ShortState(WorkspaceStore(contract_workspace))
        )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="capacity-test",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=1,
        work_control=control,
        compaction_brief=initial[-1],
    )
    return chat.runtime.runner, runtime


async def _seed_runner_contract(runner, runtime, session, initial):
    definitions = await runner.main_contract.definitions()
    runtime = replace(runtime, fixed_tools=definitions)
    revision = getattr(runner._models, "profile_revision", None)
    session.contract = hashlib.sha256(
        json.dumps(
            [
                repr(definitions),
                asdict(runtime.runtime_config.llm),
                asdict(runtime.runtime_config.web),
                revision(runner._task) if callable(revision) else "legacy",
                [(item.role, item.content) for item in initial if item.role == "system"],
            ],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    await session.save("paired")
    sequence = session.transcript.request()
    request = ChatRequest(
        messages=sequence.messages,
        continuation=sequence.continuation,
        continuation_items=sequence.items,
        request_chain_id=session.transcript.chain_id,
        model=runtime.runtime_config.llm.model or "fake",
        temperature=runtime.runtime_config.llm.temperature,
        max_output_tokens=runtime.runtime_config.llm.max_output_tokens,
        thinking_enabled=runtime.runtime_config.llm.thinking_enabled,
        tools=definitions,
        tool_choice="auto",
    )
    return runtime, request


@pytest.mark.asyncio
async def test_old_negative_steer_survives_three_compactions_and_two_restarts(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    original_work_id = control.current["id"]
    constraint = "Do not publish a release or resend any delivered artifact."
    identity = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        "event:old-negative-steer",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    assert await control.repository.prepare_input(identity, {"text": constraint})
    for message in await control.take_inputs("steer-attempt"):
        session.transcript.append(message)
    await session.save("paired")
    await control.repository.consume(control.lease, "steer-attempt")

    chains = {session.transcript.chain_id}
    for index in range(3):
        _grow(session.transcript)
        candidate = await session.compact(await session_summary(session))
        capsule = json.loads(candidate.request().messages[-1].content)
        assert capsule["task_material"]["directives"][0]["text"] == constraint
        assert capsule["task_material"]["recent_inputs"] == [
            {
                "input_id": identity,
                "event_id": 1,
                "source_key": "event:old-negative-steer",
                "text": constraint,
            }
        ]
        assert candidate.chain_id not in chains
        chains.add(candidate.chain_id)
        assert session.input_ids == [identity]
        assert control.current["id"] == original_work_id
        if index < 2:
            session = WorkSession(control, "capacity-contract")
            control.session = session
            restored = await session.restore(TurnTranscript(initial), compaction_brief=initial[-1])
            assert restored.request().messages[:2] == initial
            assert (
                json.loads(restored.request().messages[-1].content)["task_material"]
                == capsule["task_material"]
            )
            assert constraint in await session.summary_source()
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_native_public_call_and_result_are_paired_after_compaction_and_restart(
    database, tmp_path
):
    control, _, initial = await _session(database, tmp_path)
    call = ToolCall("original-call", ToolFunction("read_probe", '{"path":"report"}'))
    output = json.dumps({"ok": True, "data": {"text": "original evidence"}})
    provider = FakeLLMProvider(
        lambda _: ChatResponse(
            "Read the original evidence",
            0,
            tool_calls=(call,),
            reasoning_content="private reasoning must stay private",
            continuation=ProviderContinuation(
                "gemini", "gemini", "test", {"opaque_signature": "never summarize this signature"}
            ),
            citations=(ResponseCitation("https://example.org/evidence", "Evidence"),),
            native_tool_events=(
                NativeToolEvent(
                    NativeToolType.WEB_SEARCH, "native-search", NativeToolStatus.COMPLETED
                ),
            ),
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    definition = ChatTool("read_probe", "Read evidence", {"type": "object"})
    backend = StubAgentBackend(
        definitions=lambda *args, **kwargs: (definition,),
        execute_call=AsyncMock(return_value=output),
        is_side_effecting=lambda *args: False,
        parallel_safe=lambda *args: False,
        exhausted=lambda *args: "",
    )
    await runner.run(initial, runtime, backend)
    backend.execute_call.assert_awaited_once()
    session = control.session
    effect_key = session.call_key(call.id)
    _grow(session.transcript)
    candidate = await session.compact(
        await session_summary(session, "Continue from the successful read.")
    )
    capsule = json.loads(candidate.request().messages[-1].content)
    assert capsule["recent_tool_rounds"] == [] and capsule["recent_raw_records"] == []
    archived = await session.journal.objects.hydrate(
        await session.journal.objects.get(capsule["previous_protocol_ref"])
    )
    round_record = archived["metadata"]["model_observations"][0]
    assert round_record["tool_calls"][0]["id"] == call.id
    assert round_record["results"] == [
        {
            "call_id": call.id,
            "name": "read_probe",
            "arguments": call.function.arguments,
            "output": output,
            "executed": True,
        }
    ]
    assert round_record["citations"][0]["url"] == "https://example.org/evidence"
    assert round_record["native_tool_events"][0]["call_id"] == "native-search"
    assert "never summarize this signature" not in candidate.request().messages[-1].content
    assert "private reasoning must stay private" not in candidate.request().messages[-1].content
    restored = WorkSession(control, session.contract)
    await restored.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    assert restored.transcript.request().messages[:2] == initial
    assert "retained_tool_rounds" not in restored.progress
    material = json.loads(restored.transcript.request().messages[-1].content)
    assert material["execution_evidence"][0]["effect_key"] == effect_key
    assert await restored.journal.effect_result(effect_key) == output
    backend.execute_call.assert_awaited_once()
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_summary", "no_improvement", "fixed_tools"])
async def test_rejected_candidate_keeps_original_paired_checkpoint(database, tmp_path, failure):
    control, session, _ = await _session(database, tmp_path)
    if failure == "fixed_tools":
        _grow(session.transcript)
    await session.save("paired")
    snapshot = await _snapshot(database, control.current["id"])
    original = session.transcript.request()
    template = None
    if failure == "fixed_tools":
        template = ChatRequest(
            messages=original.messages,
            tools=(ChatTool("large_fixed_contract", "x" * 120000, {"type": "object"}),),
        )
    error = ValueError if failure == "invalid_summary" else WorkCapacityError
    # The business-resume guidance grew with explicit cumulative-note semantics;
    # the default short summary can now genuinely shrink this small transcript.
    # Keep testing rejection with a valid summary that retains the entire public
    # request instead of assuming a particular guidance length cannot improve.
    summary = (
        " "
        if failure == "invalid_summary"
        else await session_summary(
            session,
            json.dumps(asdict(original), ensure_ascii=False)
            if failure == "no_improvement"
            else "Continue the original task.",
        )
    )
    with pytest.raises(error):
        await session.compact(
            summary,
            target_tokens=20000,
            ceiling_tokens=20000,
            request_template=template,
        )
    assert session.transcript.request() == original
    assert await _snapshot(database, control.current["id"]) == snapshot
    restored = WorkSession(control, session.contract)
    await restored.restore(TurnTranscript((ChatMessage("user", "fresh wakeup"),)))
    assert restored.transcript.request().messages[0].content == "fresh wakeup"
    assert await _snapshot(database, control.current["id"]) == snapshot
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_auxiliary_output_reservation_rejects_source_before_dispatch(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "x" * 12000))
    await session.save("paired")
    snapshot = await _snapshot(database, control.current["id"])
    _, runtime = await _runtime(
        database,
        control,
        initial,
        FakeLLMProvider(),
        work_compaction_max_output_tokens=32768,
        work_context_window_tokens=65536,
    )
    # The main request fits, but this model cannot reserve the configured
    # auxiliary output even with an empty source. Paging cannot fix that.
    capacity = ModelCapacity(context_tokens=20000, output_tokens=4096)
    executor = SimpleNamespace(
        capacity=lambda _: capacity,
        # P10's explicit capacity projection replaces the duck-typed fallback.
        capacity_request=lambda _task, request: request,
        execute=AsyncMock(),
        structured_output_mode=lambda _: StructuredOutputMode.TEXT_JSON,
    )
    runner = AgentRunner(executor, ConcurrencyManager(1))
    main_request = ChatRequest(
        messages=session.transcript.request().messages, max_output_tokens=4096
    )
    assert estimate_request_tokens(main_request) < capacity.input_budget(65536, output_tokens=4096)
    with pytest.raises(WorkCapacityError, match="work_compaction_source_capacity"):
        await runner._compact_work(
            runtime,
            ModelExecutionPriority.FOREGROUND,
            capacity.input_budget(65536, output_tokens=4096),
            main_request,
        )
    executor.execute.assert_not_awaited()
    assert await _snapshot(database, control.current["id"]) == snapshot
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 0 and row["tool_calls"] == 0
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_tool_dense_source_compacts_without_duplicate_outputs(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    for index in range(70):
        call = ToolCall(f"read-{index}", ToolFunction("read_file", '{"path":"report"}'))
        output = f"evidence-{index}:" + "x" * 4500
        session.transcript.append(ChatMessage("assistant", "Read evidence", tool_calls=(call,)))
        session.transcript.append_result(call.id, output)
        session.progress.setdefault("model_observations", []).append(
            {
                "sequence": index,
                "content": "Read evidence",
                "tool_calls": [
                    {
                        "id": call.id,
                        "function": {"name": "read_file", "arguments": call.function.arguments},
                    }
                ],
                "results": [{"call_id": call.id, "output": output, "executed": True}],
            }
        )
    await session.save("paired")
    runner, runtime = await _runtime(database, control, initial, FakeLLMProvider())
    main = ChatRequest(messages=session.transcript.request().messages, max_output_tokens=8192)
    assert 108800 < estimate_request_tokens(main) < 128000

    async def summarize(_task, request, **_kwargs):
        return ChatResponse(summary_json(request.messages[-1].content), 0)

    auxiliary = AsyncMock(side_effect=summarize)
    runner._models = SimpleNamespace(
        capacity=lambda _: ModelCapacity(),
        capacity_request=lambda _task, request: request,
        execute=auxiliary,
        structured_output_mode=lambda _: StructuredOutputMode.TEXT_JSON,
    )
    candidate = await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    request = auxiliary.call_args.args[1]
    assert request.structured_output is True
    assert request.response_format is None
    assert estimate_request_tokens(request) < 128000
    assert request.messages[-1].content.count("evidence-0:") == 1
    assert candidate.chain_id != main.request_chain_id
    assert estimate_request_tokens(ChatRequest(messages=candidate.request().messages)) < 64000
    await control.repository.release(control.lease)


async def _steer(control, session, index, text):
    identity = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        f"steer:{index}",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    assert await control.repository.prepare_input(identity, {"text": text})
    attempt = f"attempt:{index}"
    for message in await control.take_inputs(attempt):
        session.transcript.append(message)
    await session.save("paired")
    await control.repository.consume(control.lease, attempt)
    return identity


@pytest.mark.asyncio
async def test_long_steer_material_stays_bounded_across_twenty_compactions_and_restart(
    database, tmp_path
):
    control, session, initial = await _session(database, tmp_path)
    identity = control.current["id"]
    source_sizes = []
    for index in range(20):
        constraint = f"Do not publish release {index}; preserve this explicit constraint."
        raw = constraint + "\n" + f"long-original-steer-{index} " * 300
        input_id = await _steer(control, session, index, raw)
        _grow(session.transcript)
        source_text = await session.summary_source()
        source = json.loads(source_text)
        assert [item["input_id"] for item in source["task_inputs"]] == [input_id]
        source_sizes.append(len(source_text))
        summary = json.loads(summary_json(source))
        summary["task_directives"][-1]["text"] = constraint
        candidate = await session.compact(json.dumps(summary))
        capsule = json.loads(candidate.request().messages[-1].content)
        material = capsule["task_material"]
        assert len(material["recent_inputs"]) <= 2
        assert material["recent_inputs"][-1]["text"] == raw
        assert len(material["directives"]) == index + 1
        assert material["directives"][0]["text"].startswith("Do not publish release 0;")
        assert candidate.request().messages[:2] == initial
        assert len(json.dumps(material).encode()) < 32768
        assert control.current["id"] == identity
        if index in {4, 11, 18}:
            session = WorkSession(control, "capacity-contract")
            control.session = session
            await session.restore(TurnTranscript(initial), compaction_brief=initial[-1])
            assert session.transcript.request().messages[:2] == initial
            assert session.progress["task_material"] == material
    # Each auxiliary request contains only one new original, two recent originals,
    # and bounded task material; it cannot grow by twenty full raw originals.
    assert source_sizes[-1] < source_sizes[2] + 25000
    async with database.sessions() as reader:
        rows = (
            await reader.execute(select(inputs.c.payload_json).where(inputs.c.work_id == identity))
        ).all()
        assert len(rows) == 20 and "long-original-steer-0" in rows[0][0]
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["reference", "directive", "input", "structure", "correction", "save"]
)
async def test_structured_summary_rejection_preserves_material_and_checkpoint(
    database, tmp_path, monkeypatch, bad
):
    control, session, _ = await _session(database, tmp_path)
    await _steer(control, session, 0, "Do not publish a release.")
    _grow(session.transcript)
    await session.compact(await session_summary(session))
    await _steer(control, session, 1, "Keep the delivered artifact ID.")
    _grow(session.transcript)
    await session.save("paired")
    snapshot = await _snapshot(database, control.current["id"])
    previous = session.transcript.request()
    previous_material = json.loads(json.dumps(session.progress["task_material"]))
    summary = json.loads(await session_summary(session))
    if bad == "reference":
        summary["pending"][0]["refs"] = ["input:999999"]
    elif bad == "directive":
        summary["task_directives"].pop(0)
    elif bad == "input":
        summary["task_directives"].pop()
    elif bad == "structure":
        summary["lifecycle"] = "completed"
    elif bad == "correction":
        summary["superseded_directives"] = [{"directive_id": "fabricated", "refs": ["goal"]}]
    elif bad == "save":
        monkeypatch.setattr(session, "save", AsyncMock(side_effect=WorkCapacityError("CAS failed")))
    with pytest.raises(WorkCapacityError):
        await session.compact(json.dumps(summary))
    assert session.transcript.request() == previous
    assert session.progress["task_material"] == previous_material
    assert await _snapshot(database, control.current["id"]) == snapshot
    restored = WorkSession(control, session.contract)
    await restored.restore(TurnTranscript((ChatMessage("user", "fresh wakeup"),)))
    assert restored.transcript.request().messages[0].content == "fresh wakeup"
    assert await _snapshot(database, control.current["id"]) == snapshot
    assert restored.progress["task_material"] == previous_material
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_explicit_steer_correction_survives_profile_boundary_and_compaction(
    database, tmp_path
):
    control, session, _ = await _session(database, tmp_path)
    await _steer(control, session, 0, "Use CSV as the required output format.")
    _grow(session.transcript)
    await session.compact(await session_summary(session))
    old = session.progress["task_material"]["directives"][0]
    identity = await _steer(control, session, 1, "Correction: output JSON instead of CSV.")
    _grow(session.transcript)
    summary = json.loads(await session_summary(session))
    summary["task_directives"].pop(0)
    summary["superseded_directives"] = [{"directive_id": old["id"], "refs": [f"input:{identity}"]}]
    await session.compact(json.dumps(summary))
    material = session.progress["task_material"]
    assert material["directives"][0]["text"] == "Correction: output JSON instead of CSV."
    assert material["corrections"][0]["previous"] == old
    changed = WorkSession(control, "new-profile-contract")
    control.session = changed
    await changed.restore(
        TurnTranscript(
            (
                ChatMessage("system", "new profile contract"),
                ChatMessage("user", "fresh wakeup"),
            )
        ),
        compaction_brief=ChatMessage("user", "fresh wakeup"),
    )
    assert changed.progress["task_material"] == material
    restored_material = json.loads(changed.transcript.request().messages[-1].content)
    assert restored_material["task_material"] == material
    assert restored_material["task_material"]["directives"][0]["text"] == (
        "Correction: output JSON instead of CSV."
    )
    _grow(changed.transcript)
    candidate = await changed.compact(await session_summary(changed))
    assert candidate.request().messages[1].content == "fresh wakeup"
    assert changed.progress["task_material"] == material
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_context_only_steer_does_not_become_a_cumulative_directive_limit(database, tmp_path):
    control, session, _ = await _session(database, tmp_path)
    await _steer(control, session, 0, "Do not publish a release.")
    _grow(session.transcript)
    await session.compact(await session_summary(session))
    directive = session.progress["task_material"]["directives"][0]
    for index in range(1, 35):
        await _steer(control, session, index, f"Continue after progress observation {index}.")
        _grow(session.transcript)
        summary = json.loads(await session_summary(session))
        summary["task_directives"].pop()
        summary["input_dispositions"][0].update(
            kind="context", reason="Progress context, no new constraint."
        )
        await session.compact(json.dumps(summary))
        assert session.progress["task_material"]["directives"] == [directive]
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,fail_second", [(17, False), (40, False), (40, True)])
async def test_one_compaction_pages_all_short_steer_and_keeps_paired_on_page_failure(
    database,
    tmp_path,
    count,
    fail_second,
):
    control, session, initial = await _session(database, tmp_path)
    identities = []
    for index in range(count):
        identities.append(
            await _steer(control, session, index, f"Continue after observation {index}.")
        )
    _grow(session.transcript)
    await session.save("paired")
    original = session.transcript.request()
    original_chain = session.transcript.chain_id
    snapshot = await _snapshot(database, control.current["id"])
    sources = []
    failed_requests = 0

    def summarize(request):
        source = json.loads(request.messages[-1].content)
        sources.append(source)
        if fail_second and len(sources) == 2:
            return "malformed second page"
        summary = json.loads(summary_json(source))
        summary["task_directives"] = []
        for item in summary["input_dispositions"]:
            item.update(kind="context", reason="Continue/progress context, no new requirement.")
        return json.dumps(summary)

    runner, runtime = await _runtime(database, control, initial, FakeLLMProvider(summarize))
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    if fail_second:
        with pytest.raises(WorkCapacityError, match="invalid_structure"):
            await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
        assert session.transcript.request() == original
        assert "task_material" not in session.progress
        saved = await _snapshot(database, control.current["id"])
        assert saved["chain_id"] == snapshot["chain_id"] and saved["phase"] == "paired"
        assert (
            json.loads(saved["payload_json"])["transcript"]
            == json.loads(snapshot["payload_json"])["transcript"]
        )
        restarted = WorkSession(control, session.contract)
        await restarted.restore(TurnTranscript((ChatMessage("user", "restart"),)))
        assert restarted.transcript.request() == original
        assert "task_material" not in restarted.progress
        control.session = restarted
        failed_requests = len(sources)
        sources.clear()
        fail_second = False
        candidate = await runner._compact_work(
            runtime, ModelExecutionPriority.FOREGROUND, 128000, main
        )
        assert restarted.progress["task_material"]["covered_input_id"] == identities[-1]
        assert candidate.chain_id != original_chain
    else:
        candidate = await runner._compact_work(
            runtime, ModelExecutionPriority.FOREGROUND, 128000, main
        )
        assert session.progress["task_material"]["covered_input_id"] == identities[-1]
        assert len(sources) == (count + 15) // 16
        assert candidate.chain_id != original_chain
        assert [
            row["input_id"] for source in sources for row in source["task_inputs"]
        ] == identities
        assert session.progress["task_material"]["directives"] == []
    if not failed_requests:
        assert sources[0].get("records")
        assert any(ref.startswith("record:") for ref in sources[0]["source_refs"])
    else:
        assert not sources[0].get("records") and not sources[0].get("effects")
        assert [
            row["input_id"] for source in sources for row in source["task_inputs"]
        ] == identities[16:]
        assert len(sources) == 2  # The already paid first page is reused after restart.
    assert all(not source.get("records") and not source.get("effects") for source in sources[1:])
    assert all("derived_observations" in source for source in sources[1:])
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == failed_requests + len(sources)
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_checkpoint", [False, True])
async def test_paged_compaction_yields_and_resumes_without_repaying_verified_pages(
    database, tmp_path, native_checkpoint
):
    from qq_ai_bot.runtime.activation_outcome import SegmentBudgetReached

    control, session, initial = await _session(database, tmp_path)
    identities = [
        await _steer(control, session, index, f"Continue item {index}.") for index in range(40)
    ]
    if native_checkpoint:
        call = {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"id": "native-read", "name": "read_probe", "args": {}},
                    "thoughtSignature": "original-private-signature",
                }
            ],
            "_call_ids": ["native-read"],
        }
        session.transcript.accept(
            ProviderContinuation(
                provider="gemini", protocol="gemini", profile_id="profile", payload=(call,)
            )
        )
        session.transcript.append_result("native-read", "original receipt")
        session.transcript.accept(
            ProviderContinuation(
                provider="gemini",
                protocol="gemini",
                profile_id="profile",
                payload=(
                    call,
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": "native-read",
                                    "name": "read_probe",
                                    "response": {"output": "original receipt"},
                                }
                            }
                        ],
                    },
                ),
            )
        )
    _grow(session.transcript)
    await session.save("paired")
    original = session.transcript.request()
    original_chain = session.transcript.chain_id
    calls = []

    def summarize(request):
        source = json.loads(request.messages[-1].content)
        calls.append(source)
        result = json.loads(summary_json(source))
        result["task_directives"] = []
        for item in result["input_dispositions"]:
            item.update(kind="context", reason="Continuation context.")
        return json.dumps(result)

    runner, runtime = await _runtime(database, control, initial, FakeLLMProvider(summarize))
    control.segment_model_limit = 2
    main = ChatRequest(
        messages=original.messages,
        continuation=original.continuation,
        continuation_items=original.items,
        max_output_tokens=8192,
    )
    with pytest.raises(SegmentBudgetReached):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    assert len(calls) == 2
    assert session.transcript.request() == original and "task_material" not in session.progress
    assert (
        session.progress["compaction_staging"]["source"]["task_material"]["covered_input_id"]
        == identities[31]
    )
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)

    async def validate():
        assert await control.repository.valid(lease)

    resumed_control = WorkControl(
        control.repository, lease, control.source_key, control.source, validate
    )
    resumed_control.current = await control.repository.get(control.current["id"])
    resumed = WorkSession(resumed_control, session.contract)
    resumed_control.session = resumed
    await resumed.restore(TurnTranscript((ChatMessage("user", "restart"),)))
    assert resumed.transcript.request() == original
    candidate = await runner._compact_work(
        replace(runtime, work_control=resumed_control),
        ModelExecutionPriority.FOREGROUND,
        128000,
        main,
    )
    assert len(calls) == 3
    assert candidate.chain_id != original_chain
    assert [item["input_id"] for page in calls for item in page["task_inputs"]] == identities
    assert resumed.progress["task_material"]["covered_input_id"] == identities[-1]
    assert "compaction_staging" not in resumed.progress
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 3 and row["tool_calls"] == 0
    await control.repository.release(lease)


@pytest.mark.asyncio
async def test_final_candidate_resumes_after_commit_failure_without_another_model_request(
    database, tmp_path, monkeypatch
):
    control, session, initial = await _session(database, tmp_path)
    await _steer(control, session, 0, "Do not publish a release.")
    _grow(session.transcript)
    await session.save("paired")
    original = session.transcript.request()
    original_chain = session.transcript.chain_id
    save = session.save

    async def fail_candidate(phase, calls=(), **kwargs):
        if session.transcript.chain_id != original_chain:
            raise WorkCapacityError("candidate CAS failed")
        await save(phase, calls, **kwargs)

    monkeypatch.setattr(session, "save", fail_candidate)
    provider = FakeLLMProvider(lambda request: summary_json(request.messages[-1].content))
    runner, runtime = await _runtime(database, control, initial, provider)
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    with pytest.raises(WorkCapacityError, match="candidate CAS failed"):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    assert session.transcript.request() == original
    assert "task_material" not in session.progress
    assert session.compaction_ready_summary is not None

    restarted = WorkSession(control, session.contract)
    await restarted.restore(TurnTranscript((ChatMessage("user", "restart"),)))
    control.session = restarted
    assert restarted.transcript.request() == original
    assert restarted.compaction_ready_summary == session.compaction_ready_summary
    candidate = await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    assert candidate.chain_id != original_chain
    assert (
        restarted.progress["task_material"]["directives"][0]["text"] == "Do not publish a release."
    )
    assert "compaction_staging" not in restarted.progress
    assert len(provider.requests) == 1
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 1
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["source", "privacy"])
@pytest.mark.parametrize("publication", ["stage", "final"])
async def test_compaction_publication_rechecks_frozen_versions_after_file_preparation(
    database, tmp_path, monkeypatch, boundary, publication
):
    from sqlalchemy import update

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
    from qq_ai_bot.runtime.subagent_repository import SubagentRepository
    from qq_ai_bot.runtime.work_repository import WorkConflict

    parent, _parent_session, initial = await _session(database, tmp_path)
    workers = SubagentRepository(parent.repository)
    identity = await workers.start(
        parent.lease,
        parent.current["id"],
        "compaction-child",
        {"goal": "prepare", "output_kind": "answer"},
    )
    lease = await workers.acquire(identity)
    assert lease is not None and lease.work_id == identity

    async def validate():
        assert await parent.repository.valid(lease)

    control = WorkControl(parent.repository, lease, "compaction-child", {}, validate)
    control.current = await parent.repository.get(identity)
    session = WorkSession(control, "capacity-contract")
    control.session = session
    await session.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    _grow(session.transcript)
    await session.save("paired")
    original = session.transcript.request()
    before = await _snapshot(database, identity)
    guard = await session._compaction_guard()
    manifest = session.journal.objects.manifest
    mutated = False

    async def prepare_then_change_boundary(value, **kwargs):
        nonlocal mutated
        prepared = await manifest(value, **kwargs)
        is_final = session.transcript.chain_id != before["chain_id"]
        if (
            not mutated
            and kwargs.get("refresh_policy") is False
            and is_final == (publication == "final")
        ):
            async with database.immediate_session() as writer:
                if boundary == "source":
                    await writer.execute(
                        update(CanonicalConversationModel)
                        .where(CanonicalConversationModel.id == lease.conversation_id)
                        .values(prompt_source_revision=guard["source_revision"] + 1)
                    )
                else:
                    state = await writer.get(ExecutionTraceStateModel, 1)
                    if state is None:
                        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
                    else:
                        state.privacy_generation += 1
            mutated = True
        return prepared

    monkeypatch.setattr(session.journal.objects, "manifest", prepare_then_change_boundary)
    provider = FakeLLMProvider(lambda request: summary_json(request.messages[-1].content))
    runner, runtime = await _runtime(database, control, initial, provider)
    with pytest.raises(WorkConflict, match="work_compaction_source_changed"):
        await runner._compact_work(
            runtime,
            ModelExecutionPriority.FOREGROUND,
            128000,
            ChatRequest(messages=original.messages, max_output_tokens=8192),
        )
    assert mutated and session.transcript.request() == original
    assert "task_material" not in session.progress
    saved = await _snapshot(database, identity)
    assert saved["chain_id"] == before["chain_id"] and saved["phase"] == "paired"
    assert (
        json.loads(saved["payload_json"])["transcript"]
        == json.loads(before["payload_json"])["transcript"]
    )
    assert (session.compaction_ready_summary is not None) == (publication == "final")
    assert len(provider.requests) == 1
    assert (await control.repository.get(identity))["model_requests"] == 1
    if boundary == "privacy":
        restarted = WorkSession(control, session.contract)
        await restarted.restore(TurnTranscript((ChatMessage("user", "restart"),)))
        assert restarted.transcript.request() == original
        await restarted.summary_source()
        assert "compaction_staging" not in restarted.progress  # Old derived candidates are stale.
    await control.repository.release(lease)
    await control.repository.release(parent.lease)


@pytest.mark.parametrize("large_anchor", [False, True])
async def test_paid_compaction_uses_soft_window_and_restored_candidate_does_not_repeat(
    database, tmp_path, monkeypatch, large_anchor
):
    control, session, initial = await _session(database, tmp_path, worker=True)
    work_id = control.current["id"]
    if large_anchor:
        task = ChatMessage("user", "Immutable original instructions. " * 10000)
        initial = (initial[0], task)
        await session.restore(TurnTranscript(initial), compaction_brief=task)
    _grow(session.transcript)
    provider = FakeLLMProvider(
        lambda request: (
            summary_json(request.messages[-1].content)
            if request.structured_output
            else "The original review is complete."
        )
    )
    runner, runtime = await _runtime(
        database,
        control,
        initial,
        provider,
        contract_workspace=tmp_path / "main-contract",
        work_context_window_tokens=524288,
        context_compaction_window_tokens=90000,
    )
    runtime, main = await _seed_runner_contract(runner, runtime, session, initial)
    call = ToolCall("original-read", ToolFunction("read_file", '{"path":"source"}'))

    async def render_receipt():
        return (
            await ToolResultBudgeter(max_characters=None).render(
                ToolExecutionResult(
                    ok=True,
                    data={"output": "Original evidence, already read once."},
                    provider_id="core",
                    tool_name="read_file",
                    mutation_committed=False,
                )
            )
        ).text

    invoke = AsyncMock(side_effect=render_receipt)
    session.transcript.append(ChatMessage("assistant", "Read evidence", tool_calls=(call,)))
    receipt = await session.execute(call, invoke, side_effecting=False)
    session.transcript.append_result(call.id, receipt)
    await session.save("paired")
    main = replace(main, messages=session.transcript.request().messages)
    effect_key = session.call_key(call.id)
    async with database.sessions() as reader:
        original_effect = dict(
            (await reader.execute(select(effects).where(effects.c.effect_key == effect_key)))
            .mappings()
            .one()
        )
    assert original_effect["state"] == "accepted"
    # Allow one paid summary and one main continuation. The artifact goal is
    # intentionally unfinished, so ordinary receipt validation keeps it open.
    runtime = replace(runtime, max_model_requests=2)
    runner._models.capacity = lambda _: ModelCapacity(input_tokens=524288)
    original = session.transcript.request()
    assert 90000 < estimate_request_tokens(runner._capacity_request(main)) < 524288
    compact = AsyncMock(wraps=runner._compact_work)
    monkeypatch.setattr(runner, "_compact_work", compact)
    result = await runner.run(initial, runtime, None)
    compact.assert_awaited_once()
    paid_requests = [request for request in provider.requests if request.structured_output]
    assert paid_requests and not provider.requests[-1].structured_output
    first_activation_requests = len(paid_requests) + 1
    assert len(provider.requests) == first_activation_requests
    assert result.model_requests == first_activation_requests
    current = await control.repository.get(work_id)
    assert current["model_requests"] == first_activation_requests and current["tool_calls"] == 1
    candidate = control.session.transcript.request()
    candidate_chain = control.session.transcript.chain_id
    assert candidate_chain != main.request_chain_id
    assert provider.requests[-1].messages[:2] == initial
    assert provider.requests[-1].tools == main.tools
    capsule = json.loads(provider.requests[-1].messages[-1].content)
    assert capsule["recent_raw_records"] == []
    archived = await session.journal.objects.hydrate(
        await session.journal.objects.get(capsule["previous_protocol_ref"])
    )
    from qq_ai_bot.runtime.work_journal import decode_transcript

    records = [
        {
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "tool_calls": [{"id": call.id} for call in message.tool_calls],
        }
        for message in decode_transcript(archived["transcript"]).request().messages
    ]
    assert any(
        retained["id"] == call.id for record in records for retained in record.get("tool_calls", [])
    )
    assert any(
        record.get("tool_call_id") == call.id and record["content"] == receipt for record in records
    )
    candidate_size = control.session.progress["compaction_request_tokens"]
    if large_anchor:
        assert candidate_size > 90000
    else:
        assert candidate_size < 90000 * runtime.runtime_config.context.work_compaction_trigger_ratio
    # The first submitted chain remains an immutable snapshot of the old history.
    assert main.messages == original.messages and main.messages[:2] == initial

    await control.repository.release(control.lease)
    from qq_ai_bot.runtime.subagent_repository import SubagentRepository

    lease = await SubagentRepository(control.repository).acquire(work_id)
    assert lease is not None, current["state"]

    async def validate():
        assert await control.repository.valid(lease)

    resumed_control = WorkControl(
        control.repository, lease, control.source_key, control.source, validate
    )
    resumed_control.current = await control.repository.get(work_id)
    # Constructing a fresh activation and Runner session exercises persisted
    # compaction progress rather than an in-memory, once-per-run flag.
    compact.reset_mock()
    result = await runner.run(
        initial, replace(runtime, work_control=resumed_control, max_model_requests=1), None
    )
    compact.assert_not_awaited()
    assert len(provider.requests) == first_activation_requests + 1
    assert not provider.requests[-1].structured_output
    assert provider.requests[-1].request_chain_id == candidate_chain
    assert provider.requests[-1].messages[: len(candidate.messages)] == candidate.messages
    assert provider.requests[-1].tools == main.tools
    current = await control.repository.get(work_id)
    assert current["model_requests"] == first_activation_requests + 1 and current["tool_calls"] == 1
    assert result.model_requests == 1 and resumed_control.current["id"] == work_id
    assert await resumed_control.session.journal.effect_result(effect_key) == receipt
    async with database.sessions() as reader:
        restored_effect = dict(
            (await reader.execute(select(effects).where(effects.c.effect_key == effect_key)))
            .mappings()
            .one()
        )
    assert restored_effect == original_effect
    invoke.assert_awaited_once()
    await control.repository.release(lease)


@pytest.mark.parametrize("within_hard_budget", [True, False])
@pytest.mark.parametrize(
    "code", ["work_compaction_source_capacity", "work_compaction_no_capacity_improvement"]
)
async def test_soft_window_compaction_failure_keeps_hard_fitting_original_request(
    database, tmp_path, monkeypatch, within_hard_budget, code
):
    control, session, initial = await _session(database, tmp_path, worker=True)
    _grow(session.transcript)
    provider = FakeLLMProvider(lambda _: "The original review is complete.")
    runner, runtime = await _runtime(
        database,
        control,
        initial,
        provider,
        contract_workspace=tmp_path / "main-contract",
        work_context_window_tokens=524288,
        context_compaction_window_tokens=90000,
    )
    runtime, main = await _seed_runner_contract(runner, runtime, session, initial)
    original = session.transcript.request()
    size = estimate_request_tokens(runner._capacity_request(main))
    assert 90000 < size < 524288
    hard_limit = 524288 if within_hard_budget else size - 1
    runner._models.capacity = lambda _: ModelCapacity(input_tokens=hard_limit)
    compact = AsyncMock(side_effect=WorkCapacityError(code))
    monkeypatch.setattr(runner, "_compact_work", compact)
    result = await runner.run(initial, runtime, None)
    compact.assert_awaited_once()
    current = await control.repository.get(control.current["id"])
    assert current["tool_calls"] == 0
    if within_hard_budget:
        assert len(provider.requests) == 1
        assert provider.requests[0].messages == original.messages
        assert provider.requests[0].request_chain_id == main.request_chain_id
        assert provider.requests[0].tools == main.tools
        assert estimate_request_tokens(provider.requests[0]) <= hard_limit
        assert current["state"] != "suspended" and current["model_requests"] == 1
        assert result.model_requests == 1
        assert control.session.progress["model_observations"][-1]["content"] == (
            "The original review is complete."
        )
    else:
        assert provider.requests == [] and current["model_requests"] == 0
        assert current["state"] == "suspended" and current["reason"] == code
        assert control.session.transcript.request() == original and result.suppress_delivery
    await control.repository.release(control.lease)


@pytest.mark.parametrize("within_hard_budget", [True, False])
async def test_paid_invalid_summary_keeps_original_receipt_and_real_budget(
    database, tmp_path, within_hard_budget
):
    control, session, initial = await _session(database, tmp_path, worker=True)
    call = ToolCall("original-operation", ToolFunction("workspace_read", "{}"))
    invoke = AsyncMock(
        return_value='{"ok":true,"data":{"run_id":"original-run","status":"succeeded"}}'
    )
    receipt = await session.execute(call, invoke, side_effecting=False)
    effect_key = session.call_key(call.id)
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    session.transcript.append_result(call.id, receipt)
    _grow(session.transcript)
    provider = FakeLLMProvider(
        lambda request: (
            '{"unexpected":true}'
            if request.structured_output
            else "The original review is complete."
        )
    )
    runner, runtime = await _runtime(
        database,
        control,
        initial,
        provider,
        contract_workspace=tmp_path / "main-contract",
        work_context_window_tokens=524288,
        context_compaction_window_tokens=90000,
    )
    runtime, main = await _seed_runner_contract(runner, runtime, session, initial)
    original = session.transcript.request()
    original_effect = await session.journal.effect_result(effect_key)
    size = estimate_request_tokens(runner._capacity_request(main))
    assert 90000 < size < 524288
    runner._models.capacity = lambda _: ModelCapacity(
        input_tokens=524288 if within_hard_budget else size - 1
    )
    result = await runner.run(initial, runtime, None)
    current = await control.repository.get(control.current["id"])
    summary_requests = [request for request in provider.requests if request.structured_output]
    main_requests = [request for request in provider.requests if not request.structured_output]
    assert len(summary_requests) == 1
    assert current["model_requests"] == result.model_requests == len(provider.requests)
    assert current["tool_calls"] == 1
    assert await control.session.journal.effect_result(effect_key) == original_effect == receipt
    invoke.assert_awaited_once()
    if within_hard_budget:
        assert len(main_requests) == 1
        assert main_requests[0].messages == original.messages
        assert main_requests[0].request_chain_id == main.request_chain_id
        assert main_requests[0].tools == main.tools
        assert current["state"] != "suspended"
    else:
        assert main_requests == []
        assert current["state"] == "suspended"
        assert current["reason"] == "work_compaction_invalid_structure"
        assert result.suppress_delivery
    await control.repository.release(control.lease)
