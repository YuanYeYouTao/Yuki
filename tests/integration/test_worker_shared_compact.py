"""Worker context maintenance uses Main dispatch and the existing root budget."""

from dataclasses import replace

from sqlalchemy import select
from tests.support.work_compaction_capacity_helpers import _runtime, _session

from yuki.domain.messages import ChatMessage
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_session import WorkSession
from yuki.services.turn_execution import TurnExecution
from yuki.services.turn_transcript import TurnTranscript
from yuki.web.models import WebMode


async def test_worker_compact_current_uses_original_child_and_root_budget(database, tmp_path):
    control, session, initial = await _session(database, tmp_path, worker=True)
    original_child = control.current["id"]
    original_root = control.current["parent_work_id"]
    original_source = control.current["source_json"]
    session.transcript.append(ChatMessage("assistant", "confirmed research " * 1000))
    provider = FakeLLMProvider(lambda _: "Verified checkpoint. Continue the original child goal.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    try:
        prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
        owner = (
            f"yuki:{control.lease.conversation_id}:"
            f"g{control.lease.generation}:work:{original_child}"
        )
        assert prepared.request.provider_session_id == owner
        compacted = await execution.compact_current(
            0, prepared.request, ModelExecutionPriority.BACKGROUND, 128000
        )
        assert len(provider.requests) == runtime.auxiliary_requests[0] == 1
        assert provider.requests[0].provider_session_id == owner
        assert provider.requests[0].messages[:-1] == prepared.request.messages
        assert provider.requests[0].tools == prepared.request.tools
        assert not any("confirmed research" in message.content for message in compacted.messages)
        assert session.progress["task_material"]["execution_checkpoint"].startswith(
            "Verified checkpoint"
        )
        saved = await control.repository.get(original_child)
        assert saved["source_json"] == original_source
        assert saved["parent_work_id"] == original_root and saved["model_requests"] == 1
        assert (await control.repository.get(original_root))["model_requests"] == 0
        async with database.sessions() as reader:
            assert (
                await reader.scalar(
                    select(budgets.c.models).where(budgets.c.root_id == original_root)
                )
                == 1
            )
        restored = WorkSession(control, session.contract)
        control.session = restored
        await restored.restore(TurnTranscript(initial))
        assert restored.progress["task_material"]["execution_checkpoint"].startswith(
            "Verified checkpoint"
        )
        assert len(provider.requests) == 1
        assert (await control.repository.get(original_child))["model_requests"] == 1
        async with database.sessions() as reader:
            assert (
                await reader.scalar(
                    select(budgets.c.models).where(budgets.c.root_id == original_root)
                )
                == 1
            )
    finally:
        await control.repository.release(control.lease)
