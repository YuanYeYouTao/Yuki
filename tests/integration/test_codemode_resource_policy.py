"""Resource changes stop original snapshots; all pools share bounded admission."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from tests.support.codemode_cases import (
    LIMITS,
    effect_rows,
    environment,
    outer_call,
    requires_worker,
    run_code,
    worker,
)

from qq_ai_bot.codemode import driver as driver_module
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.capacity import runtime_capacity
from qq_ai_bot.codemode.driver import CodeCompositionYield, CodeModeDriver
from qq_ai_bot.codemode.engine_monty import MontyEngine

pytestmark = requires_worker


async def test_unicode_stdout_capacity_is_bytes_across_business_suspensions(database, tmp_path):
    env = await environment(database, tmp_path)
    env.host.limits = replace(env.host.limits, max_output_bytes=4096)
    result, _ = await run_code(
        env, "print('中文' * 600)\nawait yuki_lookup({})\nprint('中文' * 600)\nprint('tail')"
    )
    assert result["status"] == "completed"
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode()) <= 4096
    assert result["stdout"].endswith("tail\n")
    assert env.domain.log == [("lookup", {})]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_memory_bytes", 16 << 20),
        ("max_feed_seconds", 1.0),
        ("max_recursion_depth", 100),
        ("max_suspensions", 128),
    ],
)
async def test_lower_vm_policy_settles_without_loading_or_recharging(
    database, tmp_path, field, value
):
    env = await environment(database, tmp_path, tool_limit=1)
    outer = outer_call(env, "await yuki_lookup({'q': 1})\nawait yuki_lookup({'q': 2})")
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    original_rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 1
    parent = next(row for row in original_rows.values() if row["kind"] == "code_composition")
    metadata = json.loads(parent["receipt_json"])["composition"]
    original_dump = await env.owner.journal.objects.get_bytes(metadata["snapshot_ref"])
    original_counters = metadata["resource_used"]
    env.host.limits = replace(env.host.limits, **{field: value})
    env.control.tools_started = 0

    def forbidden_load(*_args):
        raise AssertionError("lower resource policy must not load the saved VM")

    env.host.engine_factory = forbidden_load
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert result["status"] == "partial"
    assert result["snapshot_reason"] == "code_engine_resource_policy_changed"
    assert env.domain.log == [("lookup", {"q": 1})]
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 1
    saved = json.loads(
        next(row for row in rows.values() if row["kind"] == "code_composition")["receipt_json"]
    )["composition"]
    assert saved["resource_used"] == original_counters
    assert await env.owner.journal.objects.get_bytes(metadata["snapshot_ref"]) == original_dump


@pytest.mark.parametrize("change", ["binary", "api", "dump_format"])
async def test_changed_snapshot_contract_never_loads_or_replays_original_children(
    database, tmp_path, monkeypatch, change
):
    env = await environment(database, tmp_path, tool_limit=1)
    outer = outer_call(env, "await yuki_lookup({'q': 1})\nawait yuki_lookup({'q': 2})")
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    original_rows, tools, root = await effect_rows(database, env.control.current["id"])
    parent = next(row for row in original_rows.values() if row["kind"] == "code_composition")
    metadata = json.loads(parent["receipt_json"])["composition"]
    original_dump = await env.owner.journal.objects.get_bytes(metadata["snapshot_ref"])
    if change == "binary":
        env.host.worker = replace(env.host.worker, sha256="0" * 64)
    elif change == "api":
        from tests.support.codemode_cases import TOOLS

        env.host.api = project(TOOLS, "different_manifest_revision")
    else:
        monkeypatch.setattr(driver_module, "DUMP_FORMAT", "future_dump_format")
    env.control.tools_started = 0

    def forbidden_load(*_args):
        raise AssertionError("an incompatible snapshot must never reach the engine")

    env.host.engine_factory = forbidden_load
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert result["status"] == "partial" and result["snapshot_reason"] == "code_api_changed"
    assert env.domain.log == [("lookup", {"q": 1})]
    _, after_tools, after_root = await effect_rows(database, env.control.current["id"])
    assert tools == root == after_tools == after_root == 1
    assert await env.owner.journal.objects.get_bytes(metadata["snapshot_ref"]) == original_dump


async def test_changed_actual_worker_bytes_settle_original_without_spawn(database, tmp_path):
    env = await environment(database, tmp_path, tool_limit=1)
    outer = outer_call(env, "await yuki_lookup({'q': 1})\nawait yuki_lookup({'q': 2})")
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    original_rows, _, _ = await effect_rows(database, env.control.current["id"])
    parent = next(row for row in original_rows.values() if row["kind"] == "code_composition")
    metadata = json.loads(parent["receipt_json"])["composition"]
    original_dump = await env.owner.journal.objects.get_bytes(metadata["snapshot_ref"])
    corrupted = tmp_path / "fault-injection-worker"
    corrupted.write_bytes(b"not the verified native artifact")
    # Keep the expected digest in the original binding. Actual file verification
    # must reject the replaced bytes before any native process can be spawned.
    env.host.worker = replace(env.host.worker, binary_path=corrupted)
    env.control.tools_started = 0
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert result["status"] == "partial"
    assert result["snapshot_reason"] == "code_engine_binary_digest_mismatch"
    assert env.domain.log == [("lookup", {"q": 1})]
    _, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 1
    assert await env.owner.journal.objects.get_bytes(metadata["snapshot_ref"]) == original_dump


async def test_background_pools_leave_a_real_native_slot_for_foreground():
    capacity = runtime_capacity(2, 1)
    async with (
        MontyEngine(worker(), LIMITS, background=True) as first,
        MontyEngine(worker(), LIMITS, background=True) as waiting,
        MontyEngine(worker(), LIMITS) as foreground,
    ):
        one, two, urgent = (
            first.run(frozenset({"lookup"})),
            waiting.run(frozenset({"lookup"})),
            foreground.run(frozenset({"lookup"})),
        )
        task = None
        try:
            assert (await one.start("lookup(1)", {})).status == "suspended"
            first_pid = one._session.worker_pid
            task = asyncio.create_task(two.start("lookup(2)", {}))
            await asyncio.sleep(0.05)
            assert not task.done() and two._session is None
            assert (await urgent.start("lookup(3)", {})).status == "suspended"
            foreground_pid = urgent._session.worker_pid
            assert first_pid != foreground_pid
            os.kill(first_pid, 0)
            os.kill(foreground_pid, 0)
            assert capacity.active == capacity.peak == 2 and capacity.background == 1
            await one.terminate()
            assert (await task).status == "suspended"
            assert capacity.active == 2 and capacity.peak == 2
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(one.terminate(), two.terminate(), urgent.terminate())
    assert capacity.active == capacity.background == 0


async def test_cancelled_capacity_wait_has_no_worker_or_leaked_reservation():
    capacity = runtime_capacity(2, 1)
    async with (
        MontyEngine(worker(), LIMITS, background=True) as first,
        MontyEngine(worker(), LIMITS, background=True) as second,
    ):
        one, two = first.run(frozenset({"lookup"})), second.run(frozenset({"lookup"}))
        try:
            await one.start("lookup(1)", {})
            waiting = asyncio.create_task(two.start("lookup(2)", {}))
            await asyncio.sleep(0.02)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert two._session is None and capacity.active == capacity.background == 1
        finally:
            await asyncio.gather(one.terminate(), two.terminate())
    assert capacity.active == capacity.background == 0


async def test_repeated_cancellation_and_concurrent_termination_release_only_once():
    capacity = runtime_capacity(2, 1)
    async with MontyEngine(worker(), LIMITS, background=True) as engine:
        run = engine.run(frozenset({"lookup"}))
        await run.start("lookup(1)", {})
        original = run._session_cm
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowOwnerExit:
            async def __aexit__(self, *args):
                entered.set()
                await release.wait()
                await original.__aexit__(*args)

        run._session_cm = SlowOwnerExit()
        first = asyncio.create_task(run.terminate())
        await entered.wait()
        second = asyncio.create_task(run.terminate())
        try:
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            assert capacity.active == capacity.background == 1
            assert not first.done() and not second.done()
        finally:
            release.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert outcomes[1] is None
        await run.terminate()
        assert capacity.active == capacity.background == 0


async def test_capacity_timeout_never_spawns_a_worker():
    limits = replace(LIMITS, max_feed_seconds=0.05, request_timeout_seconds=0.1)
    capacity = runtime_capacity(2, 1)
    async with (
        MontyEngine(worker(), LIMITS, background=True) as first,
        MontyEngine(worker(), limits, background=True) as second,
    ):
        one, two = first.run(frozenset({"lookup"})), second.run(frozenset({"lookup"}))
        try:
            await one.start("lookup(1)", {})
            outcome = await two.start("lookup(2)", {})
            assert outcome.status == "failed"
            assert outcome.failure.category == "limit_wait_queue"
            assert two._session is None
            assert capacity.active == capacity.background == 1
        finally:
            await asyncio.gather(one.terminate(), two.terminate())
    assert capacity.active == capacity.background == 0
