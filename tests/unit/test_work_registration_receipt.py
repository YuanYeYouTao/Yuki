"""Original registration and response boundaries retain facts across recovery."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.sql.dml import Insert
from tests.support.agent_backend import StubAgentBackend
from tests.support.work_runner_helpers import case, run, tool
from tests.unit.test_subagent_lifecycle import worker_case

from qq_ai_bot.capabilities.coordinator import CoordinatedToolResult
from qq_ai_bot.capabilities.media import MediaResultText
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ChatResponse, ChatTool
from qq_ai_bot.model_runtime.models import ModelCapability
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.effect_outcomes import current_result_capture
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import CODE_COMPOSITION_YIELDED
from qq_ai_bot.services.turn_transcript import TurnTranscript


class RegistrationCrash(BaseException):
    pass


async def _restore(probe, *, source=None):
    original = probe.control
    recovered = WorkControl(
        probe.repository,
        original.lease,
        original.source_key,
        source or original.source,
        original.validate,
        current=await probe.repository.get(original.current["id"]),
    )
    result = await probe.runner.run(
        (ChatMessage("user", "resume original paid response"),),
        replace(probe.runtime, work_control=recovered),
        probe.backend,
    )
    return result, recovered


@pytest.mark.parametrize("new_trigger", [False, True])
async def test_first_registration_recovers_original_receipt_without_accepting_new_work(
    database, tmp_path, monkeypatch, new_trigger
):
    calls = (
        tool("task_control", {"action": "accept", "goal": "original new goal"}, "first-accept"),
        tool("task_control", {"action": "complete"}, "complete"),
        tool("send_message", {"text": "original tail"}, "send"),
    )
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    probe.control.current = None
    probe.control.source_key = "truly-new-source"
    execute = probe.runner._execute_tool_batch_impl
    armed = True

    async def interrupted(pending, *args, **kwargs):
        nonlocal armed
        if armed and pending[0].id == "send":
            armed = False
            raise RegistrationCrash()
        return await execute(pending, *args, **kwargs)

    monkeypatch.setattr(probe.runner, "_execute_tool_batch_impl", interrupted)
    with pytest.raises(RegistrationCrash):
        await run(probe)
    original_key = probe.control.session.call_key("first-accept")
    assert await probe.control.session.journal.effect_state(original_key) == "accepted"
    source = dict(probe.control.source)
    if new_trigger:
        await probe.env.service.writer.append(
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="later-input",
            sender_user_id="10001",
            direction="inbound",
            content="a real later request",
        )
        async with database.sessions() as reader:
            source["trigger_event_id"] = await reader.scalar(
                select(ChatEventModel.id).where(ChatEventModel.platform_message_id == "later-input")
            )
    async with database.sessions() as reader:
        before = set(await reader.scalars(select(work.c.id)))
    result, recovered = await _restore(probe, source=source)
    receipt = json.loads(await recovered.session.journal.effect_result(original_key))
    assert receipt["ok"] and receipt["work_id"] == probe.control.current["id"]
    assert result.work_state == "completed"
    assert recovered.handoff_work_id is None
    assert probe.observed == ["send_message"]
    assert len(probe.provider.requests) == 1
    async with database.sessions() as reader:
        assert set(await reader.scalars(select(work.c.id))) == before
        fact = (
            (await reader.execute(select(effects).where(effects.c.effect_key == original_key)))
            .mappings()
            .one()
        )
        usage = (
            (
                await reader.execute(
                    select(budgets).where(budgets.c.root_id == probe.control.current["id"])
                )
            )
            .mappings()
            .one()
        )
    invocation = json.loads(fact["receipt_json"])["invocation"]
    assert invocation["operation_id"] == original_key
    assert invocation["owner_execution_id"] == probe.control.current["id"]
    assert invocation["provider_call_id"] == "first-accept"
    assert invocation["dispatch_started"] is True and invocation["budget_admitted"] is False
    assert usage["models"] == 1 and usage["tools"] == 1


async def test_registration_and_original_receipt_roll_back_as_one_writer(
    database, tmp_path, monkeypatch
):
    response = ChatResponse(
        "",
        0,
        tool_calls=(tool("task_control", {"action": "accept", "goal": "new goal"}, "accept"),),
    )
    probe = await case(database, tmp_path, [response])
    probe.control.current = None
    probe.control.source_key = "atomic-registration"
    original_writer = database.immediate_session
    saw_admitted_work = False

    @asynccontextmanager
    async def interrupted_writer():
        async with original_writer() as writer:
            execute = writer.execute

            async def before_receipt(statement, *args, **kwargs):
                nonlocal saw_admitted_work
                if isinstance(statement, Insert) and statement.table is effects:
                    saw_admitted_work = bool(
                        await writer.scalar(
                            select(work.c.id).where(work.c.source_key == "atomic-registration")
                        )
                    )
                    raise RegistrationCrash()
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(writer, "execute", before_receipt)
            yield writer

    monkeypatch.setattr(database, "immediate_session", interrupted_writer)
    with pytest.raises(RegistrationCrash):
        await run(probe)
    assert saw_admitted_work is True and probe.control.current is None
    assert await probe.repository.by_source("atomic-registration") is None
    async with database.sessions() as reader:
        assert list(await reader.scalars(select(effects.c.effect_key))) == []
        assert list(await reader.scalars(select(budgets.c.root_id))) == []


async def test_registration_commit_retains_original_paid_model_usage_before_next_statement(
    database, tmp_path, monkeypatch
):
    response = ChatResponse(
        "",
        0,
        tool_calls=(tool("task_control", {"action": "accept", "goal": "new goal"}, "accept"),),
    )
    probe = await case(database, tmp_path, [response])
    probe.control.current = None
    probe.control.source_key = "paid-registration"
    original_writer = database.immediate_session
    armed = True

    @asynccontextmanager
    async def interrupted_writer():
        nonlocal armed
        recorded = False
        async with original_writer() as writer:
            execute = writer.execute

            async def track_receipt(statement, *args, **kwargs):
                nonlocal recorded
                result = await execute(statement, *args, **kwargs)
                if isinstance(statement, Insert) and statement.table is effects:
                    recorded = True
                return result

            monkeypatch.setattr(writer, "execute", track_receipt)
            yield writer
        if armed and recorded:
            armed = False
            raise RegistrationCrash()

    monkeypatch.setattr(database, "immediate_session", interrupted_writer)
    with pytest.raises(RegistrationCrash):
        await run(probe)
    row = await probe.repository.by_source("paid-registration")
    assert row is not None and row["model_requests"] == 1
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(budgets.c.models).where(budgets.c.root_id == row["id"])) == 1
        )
    # This crash lost the in-memory response before journal publication. Its
    # already paid request must not become a fresh, uncharged model purchase.
    recovered = WorkControl(
        probe.repository,
        probe.control.lease,
        probe.control.source_key,
        probe.control.source,
        probe.control.validate,
        current=row,
    )
    await probe.runner.run(
        (ChatMessage("user", "resume original paid response"),),
        replace(probe.runtime, work_control=recovered),
        probe.backend,
    )
    assert len(probe.provider.requests) == 1
    assert recovered.current["model_requests"] == 1


async def test_new_response_may_reuse_provider_id_without_losing_its_undispatched_call(
    database, tmp_path, monkeypatch
):
    first = ChatResponse("", 0, tool_calls=(tool("write_fixture", {"step": 1}, "same-id"),))
    second = ChatResponse(
        "",
        0,
        tool_calls=(
            tool("task_control", {"action": "complete"}, "complete"),
            tool("write_fixture", {"step": 2}, "same-id"),
            tool("send_message", {"text": "original tail"}, "send"),
        ),
    )
    probe = await case(database, tmp_path, [first, second])
    execute = probe.runner._execute_tool_batch_impl
    armed = True

    async def interrupted(pending, *args, **kwargs):
        nonlocal armed
        if (
            armed
            and pending[0].id == "same-id"
            and json.loads(pending[0].function.arguments)["step"] == 2
        ):
            armed = False
            raise RegistrationCrash()
        return await execute(pending, *args, **kwargs)

    monkeypatch.setattr(probe.runner, "_execute_tool_batch_impl", interrupted)
    with pytest.raises(RegistrationCrash):
        await run(probe)
    original_key = probe.control.session.call_key("same-id")
    saved = await probe.control.session.journal.load(
        probe.control.lease,
        probe.control.current["id"],
        probe.control.session.contract,
        source_control=probe.control,
    )
    pending = json.loads(saved.record["payload_json"])["pending"]
    assert [call["id"] for call in pending] == ["complete", "same-id", "send"]
    result, recovered = await _restore(probe)
    assert result.work_state == "completed"
    assert probe.observed == ["write_fixture", "write_fixture", "send_message"]
    assert await recovered.session.journal.effect_state(original_key) == "accepted"
    assert len(probe.provider.requests) == 2


async def _media_recovery_case(database, tmp_path, monkeypatch, *, new_image=False, code=False):
    calls = (
        tool("task_control", {"action": "update", "goal": "original child goal"}, "update"),
        tool("write_fixture", {}, "prefix-image"),
        *((tool("write_fixture", {}, "new-image"),) if new_image else ()),
        *((tool("execute_code", {"code": "1"}, "code"),) if code else ()),
        tool("send_message", {"text": "original tail"}, "last-tail"),
    )
    probe, _parent, _workers, _identity = await worker_case(database, tmp_path, calls)
    if code:
        probe.runtime = replace(
            probe.runtime,
            fixed_tools=(*probe.runtime.fixed_tools, ChatTool("execute_code", "code", {})),
        )
    pixel = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZKsAAAAASUVORK5CYII="
    )
    images = {
        identity: ChatImage(pixel, source="tool", tool_handle=identity)
        for identity in ("prefix-image", "new-image")
    }
    executed = []

    async def execute(invocation):
        executed.append(invocation.call.id)
        result = json.dumps({"ok": True, "data": {"status": "succeeded"}})
        if invocation.call.id not in images:
            return result
        selected = (images[invocation.call.id],)
        capture = current_result_capture.get()
        capture.outcome = ToolExecutionResult(
            ok=True,
            provider_id="fixture",
            tool_name="write_fixture",
            data={"status": "succeeded"},
            images=selected,
        )
        return MediaResultText(result, selected)

    probe.backend = StubAgentBackend(
        execute_call=execute,
        parallel_safe=lambda *_: False,
        is_side_effecting=lambda *_: True,
    )
    capabilities = probe.runner._models.capabilities
    monkeypatch.setattr(
        probe.runner._models,
        "capabilities",
        lambda task: capabilities(task) | {ModelCapability.IMAGE_INPUT},
    )
    admit = probe.repository.admit_dispatch
    armed = True
    interrupted_id = "new-image" if new_image else "last-tail"

    async def interrupted_admission(lease, work_id, key, **kwargs):
        nonlocal armed
        if armed and key.endswith(f":{interrupted_id}"):
            armed = False
            raise RegistrationCrash()
        return await admit(lease, work_id, key, **kwargs)

    monkeypatch.setattr(probe.repository, "admit_dispatch", interrupted_admission)
    with pytest.raises(RegistrationCrash):
        await run(probe)
    return probe, executed, images


async def _restore_worker_response(probe):
    original = probe.control
    recovered = WorkControl(
        probe.repository,
        original.lease,
        original.source_key,
        original.source,
        original.validate,
        current=await probe.repository.get(original.current["id"]),
    )
    session = WorkSession(recovered, original.session.contract)
    recovered.session = session
    transcript = await session.restore(TurnTranscript((ChatMessage("user", "resume"),)))
    return recovered, session, transcript


def _assert_media_after_all_results(transcript, expected_images):
    entries = transcript.portable_entries()
    receipts = [
        (index, entry.tool_call_id)
        for index, entry in enumerate(entries)
        if isinstance(entry, ChatMessage) and entry.role == "tool"
    ]
    media = [
        (index, image)
        for index, entry in enumerate(entries)
        if isinstance(entry, ChatMessage)
        for image in entry.images
    ]
    assert len({identity for _index, identity in receipts}) == len(receipts)
    assert [image for _index, image in media] == expected_images
    assert all(index > max(position for position, _identity in receipts) for index, _image in media)


@pytest.mark.parametrize("new_image", [False, True])
async def test_recovered_worker_pairs_complete_response_before_prefix_and_new_tool_media(
    database, tmp_path, monkeypatch, new_image
):
    probe, executed, images = await _media_recovery_case(
        database, tmp_path, monkeypatch, new_image=new_image
    )
    recovered, session, transcript = await _restore_worker_response(probe)
    assert not any(
        entry.images for entry in transcript.portable_entries() if isinstance(entry, ChatMessage)
    )
    assert (
        await probe.runner._resume_tool_response(
            session.pending,
            transcript,
            probe.backend,
            replace(probe.runtime, work_control=recovered),
            probe.runtime.fixed_tools,
        )
        is False
    )
    expected = [images["prefix-image"]]
    if new_image:
        expected.append(images["new-image"])
    _assert_media_after_all_results(transcript, expected)
    assert executed == ["prefix-image", *(("new-image",) if new_image else ()), "last-tail"]


async def test_recovered_worker_defers_all_tool_media_across_another_code_segment(
    database, tmp_path, monkeypatch
):
    probe, executed, images = await _media_recovery_case(
        database, tmp_path, monkeypatch, new_image=True, code=True
    )
    execute = probe.runner._execute_tool_batch_impl
    remaining_code_yields = 2

    async def code_boundary(pending, *args, **kwargs):
        nonlocal remaining_code_yields
        if pending[0].id != "code":
            return await execute(pending, *args, **kwargs)
        # This unit case exercises the caller's actual Code yield signal. Native
        # worker execution and its writer are covered by Code lifecycle tests.
        if remaining_code_yields:
            remaining_code_yields -= 1
            await kwargs["runtime"].work_control.session.save("response", kwargs["batch_calls"])
            return CoordinatedToolResult(((pending[0], CODE_COMPOSITION_YIELDED, True),), 0)
        return CoordinatedToolResult(
            ((pending[0], '{"ok":true,"data":{"output":"done"}}', True),), 0
        )

    async def intercept(pending, backend, runtime, **kwargs):
        return await code_boundary(pending, backend, runtime=runtime, **kwargs)

    monkeypatch.setattr(probe.runner, "_execute_tool_batch_impl", intercept)
    for _segment in range(2):
        recovered, session, transcript = await _restore_worker_response(probe)
        assert (
            await probe.runner._resume_tool_response(
                session.pending,
                transcript,
                probe.backend,
                replace(probe.runtime, work_control=recovered),
                probe.runtime.fixed_tools,
            )
            is True
        )
        assert not any(
            entry.images
            for entry in transcript.portable_entries()
            if isinstance(entry, ChatMessage)
        )
        assert executed == ["prefix-image", "new-image"]
        probe.control = recovered
    recovered, session, transcript = await _restore_worker_response(probe)
    assert (
        await probe.runner._resume_tool_response(
            session.pending,
            transcript,
            probe.backend,
            replace(probe.runtime, work_control=recovered),
            probe.runtime.fixed_tools,
        )
        is False
    )
    _assert_media_after_all_results(transcript, [images["prefix-image"], images["new-image"]])
    assert executed == ["prefix-image", "new-image", "last-tail"]
