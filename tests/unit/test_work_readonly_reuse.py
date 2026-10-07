"""Original readonly receipt links survive aliases/cache before paired commit."""

import json
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.work_session import WorkSession
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.capabilities.media import MediaResultText
from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.services.turn_transcript import TurnTranscript


class Crash(RuntimeError):
    pass


class Backend:
    def __init__(self, *, fail=False):
        self.invoked = 0
        self.fail = fail
        self.image = ChatImage("data:image/png;base64,original-pixels", source="tool")

    def begin_batch(self, *_args):
        pass

    def is_side_effecting(self, *_args):
        return False

    def parallel_safe(self, *_args):
        return True

    # Keep upstream assertions while using the typed invocation contract.
    def counts_toward_limit(self, *_args):
        return True

    async def execute_call(self, invocation):
        self.invoked += 1
        if self.fail:
            raise Crash("before accepted")
        return MediaResultText('{"ok":true}', (self.image,))


async def _setup(database, tmp_path):
    control = await _control(database, tmp_path, worker=True)
    session = WorkSession(control, "reuse-test")
    control.session = session
    await session.restore(TurnTranscript((ChatMessage("user", "select exact pixels"),)))
    session.sequence = 1
    runner = build_harness(database, make_settings(database.url)).processor._chat.runtime.runner
    return control, session, runner, SimpleNamespace(work_control=control, script_api=None)


async def _batch(runner, calls, backend, runtime, cache):
    return await runner._execute_tool_batch_impl(
        calls,
        backend,
        runtime,
        remaining_calls=10,
        max_parallel_calls=2,
        reusable_results=cache,
        cacheable_names=frozenset({"inspect"}),
        declared_names=frozenset({"inspect"}),
    )


def _call(identity, arguments='{"file":"selected"}'):
    return ToolCall(identity, ToolFunction("inspect", arguments))


@pytest.mark.asyncio
async def test_alias_accepted_then_crash_before_paired_preserves_original_pixels(
    database, tmp_path, monkeypatch
):
    control, session, runner, runtime = await _setup(database, tmp_path)
    backend = Backend()
    calls = (_call("representative"), _call("alias"))
    session.transcript.append(ChatMessage("assistant", tool_calls=calls))
    original = runner._tool_coordinator.execute_batch

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise Crash("accepted but not paired")

    monkeypatch.setattr(runner._tool_coordinator, "execute_batch", crash)
    with pytest.raises(Crash):
        await _batch(runner, calls, backend, runtime, {})
    restored = await WorkSession(control, "reuse-test").restore(TurnTranscript(()))
    messages = restored.request().messages
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["representative", "alias"]
    assert all(json.loads(m.content)["ok"] for m in messages if m.role == "tool")
    assert [image for m in messages for image in m.images] == [backend.image]
    assert backend.invoked == control.tools_started == 1


@pytest.mark.asyncio
async def test_cross_batch_cache_links_original_receipt_without_budget_or_reread(
    database, tmp_path, monkeypatch
):
    control, session, runner, runtime = await _setup(database, tmp_path)
    backend, cache = Backend(), {}
    first = (_call("original"),)
    session.transcript.append(ChatMessage("assistant", tool_calls=first))
    result = await _batch(runner, first, backend, runtime, cache)
    for call, receipt, _ in result.calls:
        session.transcript.append_result(call.id, receipt)
    session.transcript.append_tool_media(tuple((c.id, r) for c, r, _ in result.calls))
    await session.save("paired")
    session.sequence += 1
    next_calls = (_call("cached"), _call("cached-alias"))
    session.transcript.append(ChatMessage("assistant", tool_calls=next_calls))
    original = runner._tool_coordinator.execute_batch

    async def crash(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result.executed_count == 0
        raise Crash("cached response not paired")

    monkeypatch.setattr(runner._tool_coordinator, "execute_batch", crash)
    with pytest.raises(Crash):
        await _batch(runner, next_calls, backend, runtime, cache)
    restored = await WorkSession(control, "reuse-test").restore(TurnTranscript(()))
    messages = restored.request().messages
    assert [m.tool_call_id for m in messages if m.role == "tool"] == [
        "original",
        "cached",
        "cached-alias",
    ]
    assert all(json.loads(m.content)["ok"] for m in messages if m.role == "tool")
    assert [image for m in messages for image in m.images] == [backend.image]
    assert backend.invoked == control.tools_started == 1


@pytest.mark.asyncio
async def test_unaccepted_representative_never_becomes_successful_alias(database, tmp_path):
    control, session, runner, runtime = await _setup(database, tmp_path)
    backend = Backend(fail=True)
    calls = (_call("unknown"), _call("alias"))
    session.transcript.append(ChatMessage("assistant", tool_calls=calls))
    with pytest.raises(ExceptionGroup):
        await _batch(runner, calls, backend, runtime, {})
    restored = await WorkSession(control, "reuse-test").restore(TurnTranscript(()))
    receipts = [json.loads(m.content) for m in restored.request().messages if m.role == "tool"]
    assert len(receipts) == 2 and all(not item["ok"] for item in receipts)
    assert not any(m.images for m in restored.request().messages)
    assert backend.invoked == control.tools_started == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effecting,arguments", [(True, '{"file":"selected"}'), (False, '{"file":"different"}')]
)
async def test_reuse_link_cannot_borrow_mutation_or_different_arguments(
    database, tmp_path, side_effecting, arguments
):
    control, session, _runner, _runtime = await _setup(database, tmp_path)
    call = _call("original")

    async def invoke():
        return '{"ok":true}'

    await session.execute(call, invoke, side_effecting=side_effecting)
    alias = _call("alias", arguments)
    session.transcript.append(ChatMessage("assistant", tool_calls=(alias,)))
    session.pending_readonly_keys = {alias.id: session.call_key(call.id)}
    await session.save("response", (alias,))
    with pytest.raises(JournalUnavailable, match="readonly_reuse_corrupt"):
        await WorkSession(control, "reuse-test").restore(TurnTranscript(()))
