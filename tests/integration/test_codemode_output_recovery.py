"""Host/SQLite tests only. Deterministic engine; no native VM or external business IO."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.support.codemode_cases import (
    build_host,
    effect_rows,
    environment,
    outer_call,
    requires_worker,
)
from tests.support.work_session import WorkSession

from qq_ai_bot.codemode.driver import CodeCompositionYield, CodeModeDriver
from qq_ai_bot.codemode.driver_types import EngineCall, EngineOutcome, HostCounters
from qq_ai_bot.codemode.engine_monty import strict_json
from qq_ai_bot.codemode.limits import CodeModeLimits
from qq_ai_bot.control_plane.json_types import freeze_json_object
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript


def test_json_boundary_preserves_deep_legal_result_and_independent_watchdog():
    value = "original evidence"
    for _ in range(65):
        value = [value]
    result = strict_json(json.loads(json.dumps(value)), limit=1000)
    frozen = freeze_json_object({"result": result})["result"]
    for _ in range(65):
        frozen = frozen[0]
    assert frozen == "original evidence"
    with pytest.raises(ValueError, match="code_value_too_large"):
        strict_json(value, limit=20)
    limits = CodeModeLimits(max_feed_seconds=10, request_timeout_seconds=5)
    assert limits.engine()["max_feed_duration_secs"] == 10
    assert limits.request_timeout_seconds == 5


class SequenceRun:
    def __init__(self, count=2, printing=True):
        self.index = 0
        self.waiting = False
        self.count = count
        self.printing = printing
        self.counters = HostCounters()

    def event(self, text=""):
        if self.index == self.count:
            return EngineOutcome("completed", output=None, stdout=text)
        return EngineOutcome(
            "suspended",
            stdout=text,
            call=EngineCall(
                "future" if self.waiting else "function",
                0,
                None if self.waiting else self.index,
                None if self.waiting else "yuki_lookup",
                args=() if self.waiting else ({"q": self.index + 1},),
                pending_call_ids=(self.index,) if self.waiting else (),
            ),
        )

    async def start(self, code, inputs):
        text = "important intermediate evidence\n" if self.printing else ""
        self.counters.output_bytes += len(text.encode())
        return self.event(text)

    async def answer(self, call_id, answer):
        self.waiting = True
        return self.event()

    async def settle(self, results):
        self.index += 1
        self.waiting = False
        return self.event()

    def dump(self):
        return b"MONTY\0" + json.dumps([self.index, self.waiting]).encode()

    async def restore(self, dump, saved, counters):
        self.index, self.waiting = json.loads(dump[6:])
        self.counters = counters
        return self.event()

    async def terminate(self):
        pass


class Engine:
    def __init__(self, count=2, printing=True):
        self.count = count
        self.printing = printing

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    def run(self, names):
        return SequenceRun(self.count, self.printing)


def install(env, count=2, printing=True):
    if getattr(env, "audit_native", False):
        assert env.host.engine_factory is None and env.host.worker is not None
        return
    env.host.worker = SimpleNamespace(execution_digest=lambda: "explicit-fake-native-round2")
    env.host.engine_factory = lambda *args: Engine(count, printing)


async def recreate(env, outer):
    old = env.control
    control = WorkControl(old.repository, old.lease, old.source_key, old.source, old.validate)
    control.current = await old.repository.get(old.current["id"])
    session = WorkSession(control, env.owner.contract)
    control.session = session
    await session.restore(
        TurnTranscript((ChatMessage("system", "test"), ChatMessage("user", "resume")))
    )
    assert len(session.pending_compositions) == 1
    assert not any(m.tool_call_id == outer.call.id for m in session.transcript.request().messages)
    new = build_host(session, env.domain)
    new.audit_native = getattr(env, "audit_native", False)
    # Production resume reconstructs a trusted Invocation on the current Host.
    # Preserve original identity/content while avoiding an old activation's counters.
    new.outer = replace(outer, context=replace(outer.context, runtime=new.agent))
    install(new)
    return new


@pytest.mark.parametrize("interrupt", ["segment_yield", "cancel_after_receipt"])
@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
async def test_stdout_survives_real_journal_recovery(
    database, tmp_path, monkeypatch, interrupt, native
):
    env = await environment(
        database, tmp_path, tool_limit=1 if interrupt == "segment_yield" else 32
    )
    env.audit_native = native
    install(env)
    outer = outer_call(
        env,
        "print('important intermediate evidence'); await yuki_lookup({'q':1}); "
        "await yuki_lookup({'q':2})",
    )
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    if interrupt == "cancel_after_receipt":
        original = WorkRepository.record_effect
        parent_task = asyncio.current_task()
        crashed = False

        async def save_then_cancel(self, key, state, receipt, **kwargs):
            nonlocal crashed
            await original(self, key, state, receipt, **kwargs)
            if receipt.get("outcome", {}).get("tool") == "lookup" and not crashed:
                crashed = True
                parent_task.cancel("after durable read receipt")
                await asyncio.sleep(0)

        monkeypatch.setattr(WorkRepository, "record_effect", save_then_cancel)
    with pytest.raises(
        CodeCompositionYield if interrupt == "segment_yield" else asyncio.CancelledError
    ):
        await CodeModeDriver(env.host, outer).run()
    assert env.domain.log == [("lookup", {"q": 1})]
    saved_rows, _, _ = await effect_rows(database, env.control.current["id"])
    parent = saved_rows[outer.identity.operation_id]
    assert parent["state"] == "prepared"
    assert json.loads(parent["receipt_json"])["composition"]["resource_used"]["output_bytes"] > 0
    env = await recreate(env, outer)
    outer = env.outer
    body = json.loads(await CodeModeDriver(env.host, outer).resume())
    _rows, tools, root = await effect_rows(database, env.control.current["id"])
    print("RECOVERY", interrupt, json.dumps(body), "DOMAIN", env.domain.log, "BUDGET", tools, root)
    assert env.domain.log == [("lookup", {"q": 1}), ("lookup", {"q": 2})]
    assert tools == root == 2
    assert body["status"] == "completed"
    assert "important intermediate evidence" in body["stdout"], (
        "Already emitted print output silently disappeared across the trusted boundary"
    )
    assert body["stdout"].count("important intermediate evidence") == 1
    # Reading an already paired parent never reprints or reexecutes its children.
    assert json.loads(await CodeModeDriver(env.host, outer).resume())["stdout"] == body["stdout"]
    assert len(env.domain.log) == 2


async def test_same_stdout_survives_without_yield(database, tmp_path):
    env = await environment(database, tmp_path)
    install(env)
    body = json.loads(await CodeModeDriver(env.host, outer_call(env, "same ordinary script")).run())
    assert body["stdout"] == "important intermediate evidence\n"
    assert body["stdout_truncated"] is False


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
async def test_truncated_stdout_survives_two_real_journal_restores(database, tmp_path, native):
    from dataclasses import replace

    env = await environment(database, tmp_path, tool_limit=1)
    env.audit_native = native
    install(env, count=3)
    env.host.limits = replace(env.host.limits, max_output_bytes=16)
    outer = outer_call(
        env,
        # Emit the retained prefix in one feed, then exceed the bounded sink
        # in the next. Native print callbacks may deliver one whole feed chunk.
        "print('original')\nawait yuki_lookup({'q':1})\nprint('x' * 100)\n"
        "await yuki_lookup({'q':2})\nawait yuki_lookup({'q':3})",
    )
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    for final in (False, True):
        previous_limits = env.host.limits
        env = await recreate(env, outer)
        outer = env.outer
        env.host.limits = previous_limits
        env.host.tool_limit = 1
        install(env, count=3)
        if not final:
            with pytest.raises(CodeCompositionYield):
                resumed_body = await CodeModeDriver(env.host, outer).resume()
                pytest.fail(f"expected another yield, got {resumed_body}")
        else:
            raw = await CodeModeDriver(env.host, outer).resume()
    body = json.loads(raw)
    assert body["stdout_truncated"] is True
    assert len(body["stdout"].encode()) <= 16
    assert body["stdout"] == ("original\n" if native else "ediate evidence\n")
    assert len(env.domain.log) == 3
    _, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 3


@pytest.mark.parametrize("limit", [1, 2000, 24000])
@pytest.mark.parametrize("kind", ["chat", "responses", "anthropic", "gemini"])
async def test_operation_summary_respects_result_limit(database, tmp_path, limit, kind):
    import httpx
    from tests.support.correctness_wire import KINDS

    from qq_ai_bot.domain.messages import ChatRequest, FunctionCallOutput, ProviderContinuation

    env = await environment(database, tmp_path, tool_limit=32)
    install(env, count=20, printing=False)
    env.host.result_limit = limit
    outer = outer_call(env, "20 sequential ordinary reads")
    raw = await CodeModeDriver(env.host, outer).run()
    body = json.loads(raw)
    print(
        "SUMMARY",
        len(raw),
        "LIMIT",
        env.host.result_limit,
        "OPS",
        len(body["operations"]),
        "BODY",
        raw,
    )
    assert len(env.domain.log) == 20
    if limit == 1:
        assert len(raw) > limit and body["ok"] is True and body["replay_forbidden"] is True
    else:
        assert len(raw) <= env.host.result_limit
    assert body["stdout"] == "" and body["stdout_truncated"] is False
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 20 and len(rows) == 21
    assert rows[outer.identity.operation_id]["state"] == "accepted"
    assert await CodeModeDriver(env.host, outer).run() == raw
    assert len(env.domain.log) == 20
    assert await effect_rows(database, env.control.current["id"]) == (rows, tools, root)
    if limit < 24000:
        assert body["operations_count"] == 20 and body["operations_truncated"]
        assert body["operations_ref"]["source"] == "original composition children"
    else:
        assert len(body["operations"]) == 20 and "operations_truncated" not in body
    cls, _, _ = KINDS[kind]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        adapter = cls(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
            **({"provider_name": "openai"} if kind == "chat" else {}),
        )
        request = ChatRequest(
            messages=(
                ChatMessage("user", "reads"),
                ChatMessage("assistant", tool_calls=(outer.call,)),
                ChatMessage("tool", raw, tool_call_id=outer.call.id),
            ),
            model="synthetic",
        )
        if kind == "responses":
            request = ChatRequest(
                messages=(ChatMessage("user", "reads"),),
                model="synthetic",
                continuation=ProviderContinuation(
                    "openai",
                    "responses",
                    (
                        {
                            "type": "function_call",
                            "id": "fc-original",
                            "call_id": outer.call.id,
                            "name": outer.call.function.name,
                            "arguments": outer.call.function.arguments,
                        },
                    ),
                ),
                continuation_items=(FunctionCallOutput(outer.call.id, raw),),
            )
        payload = adapter._build_payload(request)

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    assert raw in list(strings(payload))


class GroupRun(SequenceRun):
    def event(self, text=""):
        if self.waiting:
            return EngineOutcome(
                "suspended",
                call=EngineCall("future", 0, None, None, pending_call_ids=tuple(range(self.count))),
            )
        if self.index == self.count:
            return EngineOutcome("completed", output="all received")
        return EngineOutcome(
            "suspended",
            call=EngineCall(
                "function", 0, self.index, "yuki_lookup", args=({"q": self.index + 1},)
            ),
        )

    async def answer(self, call_id, answer):
        self.index += 1
        self.waiting = self.index == self.count
        return self.event()

    async def settle(self, results):
        self.waiting = False
        return self.event()


class GroupEngine(Engine):
    def run(self, names):
        return GroupRun(self.count, False)


@pytest.mark.parametrize("root_limit", [None, 2])
async def test_bounded_concurrent_reads_keep_exact_budget_and_partial_receipts(
    database, tmp_path, root_limit
):
    from sqlalchemy.dialects.sqlite import insert

    from qq_ai_bot.runtime.work_budget_schema import budgets

    env = await environment(database, tmp_path, max_parallel=2)
    install(env, count=5, printing=False)
    env.host.engine_factory = lambda *args: GroupEngine(5, False)
    identity = env.control.current["id"]
    if root_limit is not None:
        async with database.sessions() as writer, writer.begin():
            await writer.execute(
                insert(budgets)
                .values(root_id=identity, tool_limit=root_limit)
                .on_conflict_do_update(
                    index_elements=[budgets.c.root_id], set_={"tool_limit": root_limit}
                )
            )
    body = json.loads(
        await CodeModeDriver(env.host, outer_call(env, "five small independent reads")).run()
    )
    _rows, tools, root = await effect_rows(database, identity)
    print(
        "CONCURRENT",
        root_limit,
        json.dumps(body),
        "DOMAIN",
        env.domain.log,
        "PEAK",
        env.domain.peak,
        "BUDGET",
        tools,
        root,
    )
    assert env.domain.peak <= 2
    assert tools == root == len(env.domain.log) == (5 if root_limit is None else 2)
    assert body["status"] == ("completed" if root_limit is None else "partial")
    assert sum(op["status"] == "succeeded" for op in body["operations"]) == len(env.domain.log)
