"""Drive current Main maintenance through its real dispatch and Work journal."""

from dataclasses import replace

from tests.support.work_compaction_capacity_helpers import _runtime

from yuki.domain.messages import ChatRequest
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.services.turn_execution import TurnExecution


async def compact_session(database, control, initial, *, public_tail=()):
    provider = FakeLLMProvider(lambda request: "Verified results; continue the original task.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, auxiliary_requests=[0], max_model_requests=20)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = control.session.transcript
    execution.state.public_tail = list(public_tail)
    request = ChatRequest(messages=execution.state.transcript.request().messages)
    await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 128000)
    return execution.state.transcript, provider
