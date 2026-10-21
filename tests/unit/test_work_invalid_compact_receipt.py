"""A settled invalid compact remains paid evidence across Work recovery."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from tests.support.work_compaction_capacity_helpers import _runtime, _session

from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.llm.base import LLMEmptyResponseError
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_session import WorkSession
from yuki.services.turn_execution import TurnExecution
from yuki.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("crash_after_response", [False, True])
async def test_invalid_paid_compact_survives_pairing_reopen_and_next_candidate(
    database, tmp_path, monkeypatch, worker, crash_after_response
):
    control, session, initial = await _session(database, tmp_path, worker=worker)
    identity = control.current["id"]
    original_source = control.current["source_json"]
    session.transcript.append(ChatMessage("assistant", "An original completed finding."))
    original = session.transcript.request()

    def empty_response(request):
        raise LLMEmptyResponseError("Confirmed empty response")

    provider = FakeLLMProvider(empty_response)
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=None, auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    request = ChatRequest(messages=original.messages, max_output_tokens=1024)
    if crash_after_response:

        async def fail_pairing():
            raise WorkConflict("pairing_failed_after_paid_response")

        monkeypatch.setattr(session, "finish_context_compaction", fail_pairing)
        with pytest.raises(WorkConflict, match="pairing_failed_after_paid_response"):
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, None)
        assert session.progress["context_compaction"]["phase"] == "response"
    else:
        assert (
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, None)
            == request
        )
        assert session.transcript.request() == original
        assert session.progress["context_compaction"]["phase"] == "paired"
    assert runtime.auxiliary_requests == [1]
    assert len(provider.requests) == 1

    await database.close()
    restored = WorkSession(control, session.contract)
    control.session = restored
    await restored.restore(TurnTranscript(initial))
    failed = deepcopy(restored.progress["context_compaction"])
    assert failed["phase"] == "paired"
    assert failed["valid"] is False
    assert failed["response"] == ""
    assert failed["error_category"] == "LLMEmptyResponseError"
    assert "model_response" not in failed
    assert json.dumps(failed["request"]["messages"][:-1], sort_keys=True) == json.dumps(
        [asdict(message) for message in request.messages], sort_keys=True
    )
    assert "An original completed finding." in str(failed["request"])
    saved = await control.repository.get(identity)
    assert saved["source_json"] == original_source
    assert (saved["model_requests"], saved["tool_calls"]) == (1, 0)

    failed["request"].update(
        {
            "request_shape_hash": "legacy-request-shape",
            "conversation_prefix_hash": "legacy-conversation-prefix",
            "prompt_snapshot_fingerprint": "legacy-snapshot",
            "static_prompt_revision": "legacy-static-prefix",
        }
    )
    restored.progress["context_compaction"] = deepcopy(failed)
    await restored.save("paired")
    await database.close()
    reopened = WorkSession(control, session.contract)
    control.session = reopened
    finish_compact = reopened.finish_context_compaction
    finish = AsyncMock(side_effect=AssertionError("Settled compact must not finish again"))
    monkeypatch.setattr(reopened, "finish_context_compaction", finish)
    await reopened.restore(TurnTranscript(initial))
    finish.assert_not_awaited()
    assert reopened.progress["context_compaction"] == failed
    assert len(provider.requests) == 1
    assert (await control.repository.get(identity))["model_requests"] == 1
    loaded = await reopened.journal.load(
        control.lease, identity, reopened.contract, source_control=control
    )
    assert loaded.record["phase"] == "paired"
    payload = json.loads(loaded.record["payload_json"])
    assert payload["metadata"]["progress"]["context_compaction"] == failed

    if worker:
        resumed = TurnExecution(runner, initial, runtime, None)
        resumed.state.transcript = reopened.transcript
        assert reopened.transcript.request().messages == original.messages
        assert (
            await resumed.compact_current(
                0, request, ModelExecutionPriority.FOREGROUND, None, trigger_tokens=96000
            )
            == request
        )
        assert len(provider.requests) == 1
        assert (await control.repository.get(identity))["model_requests"] == 1
        assert reopened.progress["context_compaction"] == failed

    monkeypatch.setattr(reopened, "finish_context_compaction", finish_compact)
    reopened.transcript.append(ChatMessage("assistant", "A newly completed finding."))
    if worker:
        fresh_request = replace(request, messages=reopened.transcript.request().messages)
        assert (
            await resumed.compact_current(
                0, fresh_request, ModelExecutionPriority.FOREGROUND, None, trigger_tokens=96000
            )
            == fresh_request
        )
        assert len(provider.requests) == 2
        assert (await control.repository.get(identity))["model_requests"] == 2
    else:
        await reopened.prepare_context_compaction(initial, ())
        assert len(provider.requests) == 1
        assert (await control.repository.get(identity))["model_requests"] == 1
    candidate = reopened.progress["context_compaction"]
    manifest = await reopened.journal.objects.get(candidate["previous_protocol_ref"])
    original_manifest = await reopened.journal.objects.hydrate(manifest)
    assert original_manifest["metadata"]["progress"]["context_compaction"] == failed
    assert "A newly completed finding." in str(original_manifest["transcript"])
    if worker:
        assert candidate["valid"] is False
    else:
        assert "valid" not in candidate
