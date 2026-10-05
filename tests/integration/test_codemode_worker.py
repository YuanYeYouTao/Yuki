"""Real pinned Monty worker: isolation, limits, snapshots and restore identity.

Set YUKI_MONTY_BINARY to the worker built by scripts/build_monty_worker.sh.
"""

import os
from pathlib import Path

import pytest
from tests.integration.test_code_boundary_publication import PARENT, child, composed
from tests.support.codemode_cases import worker

from qq_ai_bot.codemode.driver_types import EngineAnswer, HostCounters
from qq_ai_bot.codemode.engine_monty import MontyEngine
from qq_ai_bot.codemode.limits import CodeModeLimits
from qq_ai_bot.codemode.snapshot_binding import load_boundary, persist_boundary

BINARY = Path(os.environ.get("YUKI_MONTY_BINARY", "/nonexistent/monty"))

try:
    import pydantic_monty  # noqa: F401

    _BINDING = True
except ImportError:
    _BINDING = False

pytestmark = pytest.mark.skipif(
    not (_BINDING and BINARY.is_file()),
    reason="pinned Monty worker/binding not built; set YUKI_MONTY_BINARY "
    "(scripts/build_monty_worker.sh)",
)

MANIFEST = frozenset({"lookup", "send_message"})
FAST = CodeModeLimits(
    max_feed_seconds=1.0,
    max_memory_bytes=16 << 20,
    max_output_bytes=4096,
    request_timeout_seconds=5.0,
)


async def run_once(code, *, limits=FAST, inputs=None, answers=()):
    async with MontyEngine(worker(), limits) as engine:
        run = engine.run(MANIFEST)
        outcome = await run.start(code, inputs or {})
        calls = []
        replies = iter(answers)
        while outcome.status == "suspended":
            calls.append(outcome.call)
            outcome = await run.answer(outcome.call.engine_call_id, next(replies))
        await run.terminate()
        return outcome, calls, run.counters


async def test_manifest_call_suspends_to_host_and_completes():
    outcome, calls, counters = await run_once(
        "r = lookup(q, limit=2)\nprint('got', r)\n{'answer': r}",
        inputs={"q": "ada"},
        answers=[EngineAnswer.ok(["x", "y"])],
    )
    assert [(c.function_name, c.args, c.kwargs) for c in calls] == [
        ("lookup", ("ada",), {"limit": 2})
    ]
    assert outcome.status == "completed" and outcome.output == {"answer": ["x", "y"]}
    assert outcome.stdout == "got ['x', 'y']\n"
    assert counters.suspensions == 1


async def test_infinite_loop_hits_engine_time_limit_and_discards_worker():
    outcome, _, _ = await run_once("while True:\n    pass")
    assert outcome.failure.category == "limit_time"
    assert outcome.failure.worker_discarded


async def test_output_flood_is_bounded_on_host():
    outcome, _, counters = await run_once("while True:\n    print('x' * 1000)")
    assert outcome.failure.category == "limit_time"
    assert len(outcome.stdout.encode()) <= FAST.max_output_bytes
    assert outcome.stdout_truncated and counters.output_bytes > FAST.max_output_bytes


async def test_memory_exhaustion_cannot_be_caught_by_script():
    outcome, _, _ = await run_once(
        "try:\n    a = [0] * 10**8\nexcept MemoryError:\n    lookup('escaped')"
    )
    assert outcome.failure.category == "limit_memory"
    assert outcome.failure.worker_discarded


async def test_oversized_code_is_refused_before_worker_start():
    outcome, _, counters = await run_once("x = 1\n" * 20000)
    assert outcome.failure.category == "limit_code" and counters.feeds == 0


async def test_long_compile_crashes_only_the_worker():
    # Within the host code cap, yet deep enough to overflow the native compiler.
    limits = CodeModeLimits(max_code_bytes=1 << 20)
    outcome, _, _ = await run_once("x = " + "+".join(["1"] * 200000), limits=limits)
    assert outcome.status == "failed" and outcome.failure.worker_discarded
    # The host keeps running and a fresh worker still serves new scripts.
    again, _, _ = await run_once("1 + 1")
    assert again.output == 2


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("secret_token", "NameError"),
        ("__import__('os')", "NameError"),
        ("open('/etc/passwd').read()", "PermissionError"),
        ("import os\nos.environ.get('HOME')", "RuntimeError"),  # Environment not supported.
        ("import pathlib\npathlib.Path('/etc/passwd').read_text()", "PermissionError"),
        ("import socket", "ModuleNotFoundError"),
        ("import subprocess", "ModuleNotFoundError"),
        ("database.execute('x')", "NameError"),
        # Manifest names exist only as suspensions, never as host objects to inspect.
        ("send_message.__globals__", "NameError"),
    ],
)
async def test_names_os_and_network_never_reach_the_host(probe, expected):
    code = (
        "try:\n" + "".join(f"    {line}\n" for line in probe.splitlines()) + "    r = 'reached'\n"
        "except BaseException as e:\n"
        "    r = type(e).__name__\n"
        "r"
    )
    outcome, calls, _ = await run_once(code)
    assert calls == []
    assert outcome.status == "completed" and outcome.output == expected


async def test_arbitrary_objects_are_refused_both_ways():
    # A lambda/set argument never reaches the manifest dispatcher.
    outcome, calls, counters = await run_once(
        "try:\n    lookup({1, 2})\nexcept TypeError as e:\n    r = 'refused'\nr"
    )
    assert calls == [] and outcome.output == "refused" and counters.denied_calls == 1
    outcome, _, _ = await run_once("class A:\n    pass\nA()")
    assert outcome.failure.category == "result_not_json"
    with pytest.raises(ValueError):
        await run_once("lookup()", answers=[EngineAnswer.ok(object())])


async def test_engine_suspension_cap_is_not_a_business_budget():
    limits = CodeModeLimits(max_feed_seconds=1.0, max_suspensions=2, request_timeout_seconds=5.0)
    outcome, calls, counters = await run_once(
        "for i in range(5):\n    lookup(i)",
        limits=limits,
        answers=[EngineAnswer.ok(None)] * 5,
    )
    assert outcome.failure.category == "limit_suspensions"
    # The Host saw each business call individually; the engine cap only stops the VM.
    assert [c.args for c in calls] == [(0,), (1,)]
    assert counters.suspensions == 2


@pytest.mark.parametrize("count", [16, 17])
async def test_pending_future_limit_is_a_typed_failure_and_does_not_damage_the_host(count):
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        outcome = await run.start(
            f"import asyncio\nr = await asyncio.gather(*[lookup(i) for i in range({count})])\nr",
            {},
        )
        while outcome.status == "suspended" and outcome.call.kind == "function":
            outcome = await run.answer(outcome.call.engine_call_id, EngineAnswer.future())
        if count == 16:
            assert outcome.status == "suspended" and outcome.call.kind == "future"
            pending = outcome.call.pending_call_ids
            assert len(pending) == count
            outcome = await run.settle({key: EngineAnswer.ok(key) for key in pending})
            assert outcome.status == "completed" and len(outcome.output) == count
        else:
            assert outcome.status == "failed"
            assert outcome.failure.category == "limit_wait_queue"
            assert "smaller awaited batches" in outcome.failure.message
            assert outcome.failure.worker_discarded
        await run.terminate()
    again, _, _ = await run_once("1 + 1")
    assert again.status == "completed" and again.output == 2


async def test_reserved_names_cannot_enter_the_manifest():
    async with MontyEngine(worker(), FAST) as engine:
        with pytest.raises(ValueError, match="code_manifest_reserved_name"):
            engine.run(frozenset({"lookup", "open"}))


async def test_answer_must_target_the_pending_call():
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        outcome = await run.start("lookup(1)", {})
        with pytest.raises(RuntimeError, match="code_answer_not_pending"):
            await run.answer(outcome.call.engine_call_id + 1, EngineAnswer.ok(1))
        await run.answer(outcome.call.engine_call_id, EngineAnswer.ok(1))
        with pytest.raises(RuntimeError, match="code_answer_not_pending"):
            await run.answer(outcome.call.engine_call_id, EngineAnswer.ok(1))
        await run.terminate()


# -- snapshots through the original Work's private ProtocolStore ----------------------


async def suspended_boundary(database, tmp_path, code):
    owner, binding = await composed(database, tmp_path)
    store = owner.journal.objects
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        outcome = await run.start(code, {})
        while outcome.call.kind == "function" and outcome.call.function_name == "lookup":
            outcome = await run.answer(outcome.call.engine_call_id, EngineAnswer.future())
        record = await persist_boundary(
            store,
            binding,
            run.dump(),
            outcome.call,
            run.counters,
            max_bytes=FAST.max_snapshot_bytes,
        )
        await run.terminate()
    await owner.control.repository.publish_code_boundary(
        owner.control.lease,
        binding.work_id,
        PARENT,
        expected_revision=0,
        composition=record.composition_fields(),
        child=child(binding.work_id),
        store=store,
        binding=binding,
        side_effecting=False,
    )
    composition = {"snapshot_ref": record.snapshot_ref, **record.composition_fields()}
    return owner, binding, store, composition, outcome.call


FUTURE_CODE = (
    "import asyncio\n"
    "a = lookup('a')\n"
    "b = lookup('b')\n"
    "x, y = await asyncio.gather(a, b)\n"
    "{'sum': x + y}"
)


async def test_future_snapshot_survives_restart_and_is_settled_manually(database, tmp_path):
    _owner, binding, store, composition, call = await suspended_boundary(
        database, tmp_path, FUTURE_CODE
    )
    assert call.kind == "future" and sorted(call.pending_call_ids) == [0, 1]
    # A new process: a new engine and run, restoring only from the private store.
    dump, _expected, counters = await load_boundary(store, binding, composition)
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        restored = await run.restore(dump, composition["boundary_call"], counters)
        assert restored.status == "suspended"
        # The same engine ids come back; nothing mints a new business identity.
        assert restored.call.pending_call_ids == call.pending_call_ids
        assert run.counters.suspensions >= counters.suspensions
        done = await run.settle({0: EngineAnswer.ok(2), 1: EngineAnswer.ok(3)})
        assert done.output == {"sum": 5}
        await run.terminate()


async def test_restored_counters_continue_from_saved_values(database, tmp_path):
    _owner, binding, store, composition, _call = await suspended_boundary(
        database, tmp_path, FUTURE_CODE
    )
    dump, _expected, counters = await load_boundary(store, binding, composition)
    saved = counters.suspensions
    assert saved > 0
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        await run.restore(dump, composition["boundary_call"], HostCounters(**counters.as_dict()))
        assert run.counters.suspensions == saved + 1  # Re-announcement counts, never resets.
        await run.terminate()


async def test_mismatched_boundary_is_a_binding_conflict(database, tmp_path):
    _owner, binding, store, composition, _call = await suspended_boundary(
        database, tmp_path, FUTURE_CODE
    )
    dump, _expected, counters = await load_boundary(store, binding, composition)
    forged = {**composition["boundary_call"], "pending_call_ids": [0]}
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        outcome = await run.restore(dump, forged, counters)
        assert outcome.failure.category == "snapshot_binding_conflict"
        assert outcome.failure.worker_discarded


@pytest.mark.parametrize(
    "tamper",
    [
        lambda dump: dump[:-16],  # Truncated.
        lambda dump: b"MONTY\x00\x01\x00" + dump[8:],  # Older dump-format version.
        lambda dump: dump[:40] + bytes(b ^ 0xFF for b in dump[40:80]) + dump[80:],
    ],
)
async def test_tampered_or_old_dump_never_resumes(database, tmp_path, tamper):
    _owner, binding, store, composition, _call = await suspended_boundary(
        database, tmp_path, FUTURE_CODE
    )
    dump, _expected, counters = await load_boundary(store, binding, composition)
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        outcome = await run.restore(tamper(dump), composition["boundary_call"], counters)
        assert outcome.status == "failed" and outcome.failure.worker_discarded
        with pytest.raises(RuntimeError):
            await run.settle({0: EngineAnswer.ok(1), 1: EngineAnswer.ok(1)})


async def test_bytes_cannot_bypass_the_private_store(database, tmp_path):
    _owner, binding, store, composition, _call = await suspended_boundary(
        database, tmp_path, FUTURE_CODE
    )
    forged = {**composition, "snapshot_ref": "0" * 64}
    with pytest.raises(ValueError):
        await load_boundary(store, binding, forged)


async def test_same_snapshot_cannot_be_dispatched_twice_on_one_run(database, tmp_path):
    _owner, binding, store, composition, _call = await suspended_boundary(
        database, tmp_path, FUTURE_CODE
    )
    dump, _expected, counters = await load_boundary(store, binding, composition)
    async with MontyEngine(worker(), FAST) as engine:
        run = engine.run(MANIFEST)
        await run.restore(dump, composition["boundary_call"], counters)
        with pytest.raises(RuntimeError, match="code_run_state_invalid"):
            await run.restore(dump, composition["boundary_call"], counters)
        await run.settle({0: EngineAnswer.ok(1), 1: EngineAnswer.ok(1)})
        with pytest.raises(RuntimeError, match="code_settle_not_pending"):
            await run.settle({0: EngineAnswer.ok(1), 1: EngineAnswer.ok(1)})
        await run.terminate()
