"""Pi loop semantics on the ported core, with fake boundaries only."""

import asyncio
import json

import pytest

from qq_ai_bot.agent_core import (
    RETRY,
    STOP,
    TRUNCATED_CALL_RECEIPT,
    AgentState,
    Continue,
    End,
    EventStream,
    ToolBatchOutcome,
    ToolCallOutcome,
    reduce,
    run_agent_loop,
)
from qq_ai_bot.agent_core.model_boundary import Callbacks, Frame, collect_response
from qq_ai_bot.domain.messages import ChatResponse, ModelResponseStatus, ToolCall, ToolFunction


def call(identity, name="read", arguments="{}"):
    return ToolCall(identity, ToolFunction(name, arguments))


class Script:
    """Deterministic boundaries; records every responsibility the core invokes."""

    def __init__(self, responses, *, steer_at=(), terminate=False):
        self.responses = list(responses)
        self.log = []
        self.steer_at = set(steer_at)
        self.terminate = terminate

    def callbacks(self):
        async def begin(index):
            self.log.append(("begin", index))

        async def steer(index):
            if index in self.steer_at:
                self.log.append(("steer", index))
            return None

        async def request(index):
            self.log.append(("request", index))
            return self.responses.pop(0)

        async def execute_tools(index, response):
            self.log.append(("execute", tuple(c.id for c in response.tool_calls)))
            return ToolBatchOutcome(
                tuple(ToolCallOutcome(c, '{"ok":true}', True) for c in response.tool_calls),
                executed_count=len(response.tool_calls),
                terminate=self.terminate,
            )

        async def settle_truncated(index, response, outcomes):
            self.log.append(("truncated", tuple((o.call.id, o.executed) for o in outcomes)))
            return Continue()

        async def settle_final(index, response):
            self.log.append(("final", response.content))
            return End(response.content)

        async def stop_before_tools(index, response):
            return None

        async def finish_tool_turn(index, response, batch):
            self.log.append(("finish", batch.executed_count))
            return Continue()

        async def exhausted():
            self.log.append(("exhausted",))
            return "exhausted"

        return Callbacks(
            begin,
            steer,
            request,
            execute_tools,
            settle_truncated,
            settle_final,
            stop_before_tools,
            finish_tool_turn,
            exhausted,
        )


async def run(script, *, max_requests=4, events=None):
    boundaries = script.callbacks()
    return await run_agent_loop(
        max_requests=max_requests,
        model=boundaries,
        invocation=boundaries,
        settlement=boundaries,
        events=events,
    )


async def test_tool_results_always_lead_to_another_request_then_final():
    script = Script(
        [ChatResponse("", 0, tool_calls=(call("a"), call("b"))), ChatResponse("done", 0)],
        steer_at={1},
    )
    assert await run(script) == "done"
    # Pi runLoop: steering is taken after tool results, before the next request.
    assert script.log == [
        ("begin", 0),
        ("request", 0),
        ("execute", ("a", "b")),
        ("finish", 2),
        ("begin", 1),
        ("steer", 1),
        ("request", 1),
        ("final", "done"),
    ]


async def test_truncated_response_fails_every_call_in_band_without_dispatch():
    truncated = ChatResponse(
        "",
        0,
        tool_calls=(call("t1", "send_message", '{"text":"ok"}'), call("t2", arguments='{"q":')),
        status=ModelResponseStatus.INCOMPLETE,
    )
    script = Script([truncated, ChatResponse("recovered", 0)])
    events = EventStream()
    assert await run(script, events=events) == "recovered"
    # Even a call whose arguments happen to parse is never executed (Pi "length").
    assert ("execute", ("t1",)) not in script.log
    assert ("truncated", (("t1", False), ("t2", False))) in script.log
    receipts = [e.result for e in events if e.type == "tool_execution_end"]
    assert receipts == [TRUNCATED_CALL_RECEIPT] * 2
    assert json.loads(TRUNCATED_CALL_RECEIPT)["mutation_committed"] is False


async def test_partial_frames_are_frozen_and_only_done_completes():
    seen = []

    async def frames(*items):
        for item in items:
            yield item

    response = await collect_response(
        frames(
            Frame("start"),
            Frame("toolcall_delta", call_id="c1", name="send_message", arguments='{"te'),
            Frame("toolcall_delta", call_id="c1", name="send_message", arguments='xt":"hi"}'),
        ),
        seen.append,
    )
    # The stream ended without "done": incomplete, so the loop would never execute it.
    assert response.status is ModelResponseStatus.INCOMPLETE
    assert response.incomplete_reason == "stream_ended"
    assert response.tool_calls[0].function.arguments == '{"text":"hi"}'
    snapshots = [e.response.tool_calls for e in seen if e.response is not None]
    # An earlier emitted partial is unchanged by later deltas.
    assert snapshots[1][0].function.arguments == '{"te'
    done = await collect_response(
        frames(Frame("start"), Frame("text_delta", text="ok"), Frame("done")), seen.append
    )
    assert done.status is ModelResponseStatus.COMPLETED and done.content == "ok"
    failed = await collect_response(frames(Frame("start"), Frame("error")), seen.append)
    assert failed.status is ModelResponseStatus.INCOMPLETE
    with pytest.raises(ValueError, match="frame_before_start"):
        await collect_response(frames(Frame("done")), seen.append)


async def test_incomplete_frames_through_loop_never_execute():
    async def frames():
        yield Frame("start")
        yield Frame("toolcall_delta", call_id="c1", name="send_message", arguments='{"text":')

    partial = await collect_response(frames(), lambda _e: None)
    script = Script([partial, ChatResponse("ok", 0)])
    assert await run(script) == "ok"
    assert not any(entry[0] == "execute" for entry in script.log)


async def test_retry_and_stop_signals_and_exhaustion():
    script = Script([RETRY, ChatResponse("", 0, tool_calls=(call("x"),)), STOP])
    assert await run(script) == "exhausted"
    assert [e for e in script.log if e[0] == "request"] == [
        ("request", 0),
        ("request", 1),
        ("request", 2),
    ]
    bounded = Script([ChatResponse("", 0, tool_calls=(call(f"x{i}"),)) for i in range(5)])
    assert await run(bounded, max_requests=2) == "exhausted"
    assert sum(entry[0] == "request" for entry in bounded.log) == 2


async def test_all_terminate_batch_stops_without_forcing_another_request():
    script = Script([ChatResponse("", 0, tool_calls=(call("x"),))], terminate=True)
    assert await run(script) == "exhausted"
    assert sum(entry[0] == "request" for entry in script.log) == 1


async def test_event_order_and_state_reduction():
    events = EventStream()
    script = Script([ChatResponse("", 0, tool_calls=(call("a"),)), ChatResponse("done", 0)])
    await run(script, events=events)
    kinds = [e.type for e in events]
    assert kinds[0] == "agent_start" and kinds[-1] == "agent_end"
    assert kinds.count("turn_start") == kinds.count("turn_end") == 2
    first_turn = kinds[: kinds.index("turn_end") + 1]
    assert first_turn == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
    ]
    state = AgentState()
    for event in events:
        state = reduce(state, event)
    assert not state.running and state.turns == 2 and state.finished_tool_calls == ("a",)
    assert state.pending_tool_calls == frozenset()


async def test_event_backpressure_drops_diagnostics_never_execution():
    failures = []

    def broken(_event):
        failures.append(1)
        raise RuntimeError("listener down")

    events = EventStream(capacity=3, listeners=(broken,))
    script = Script(
        [ChatResponse("", 0, tool_calls=(call("a"), call("b"))), ChatResponse("done", 0)]
    )
    assert await run(script, events=events) == "done"
    assert ("execute", ("a", "b")) in script.log
    assert events.dropped > 0 and len(tuple(events)) == 3
    assert events.listener_failures == len(failures) > 0
    assert tuple(events)[-1].type == "agent_end"


async def test_cancellation_still_ends_the_event_stream():
    started = asyncio.Event()

    class Hanging(Script):
        def callbacks(self):
            base = super().callbacks()

            async def request(index):
                started.set()
                await asyncio.Event().wait()

            return Callbacks(
                **{**{f: getattr(base, f) for f in base.__slots__}, "request": request}
            )

    events = EventStream()
    task = asyncio.create_task(run(Hanging([]), events=events))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e.type for e in events][-1] == "agent_end" and events.ended


async def test_core_has_no_platform_or_database_dependencies():
    import ast
    from pathlib import Path

    import qq_ai_bot.agent_core as core

    allowed = {"qq_ai_bot.agent_core", "qq_ai_bot.domain.messages"}
    for path in Path(core.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("qq_ai_bot")
            ):
                assert any(node.module.startswith(prefix) for prefix in allowed), (
                    path,
                    node.module,
                )
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("qq_ai_bot") for a in node.names), path
