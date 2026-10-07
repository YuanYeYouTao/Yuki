"""Observe a real Linux worker's namespaces, mounts, UID, limits and termination.

Run as the unprivileged Host on the isolated validation machine. Only the fixed
native worker and manual suspend API are used; this script starts no Bot and
reads no application database/configuration or model credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def descendants(pid: int) -> list[int]:
    result = [pid]
    for parent in result:
        try:
            children = (Path(f"/proc/{parent}/task/{parent}/children")).read_text().split()
        except FileNotFoundError:
            continue
        result.extend(int(child) for child in children)
    return result


def observe(pool_pid: int) -> tuple[int, dict[str, Any]]:
    candidates = descendants(pool_pid)
    worker = next(pid for pid in candidates if Path(f"/proc/{pid}/exe").readlink().name == "monty")
    root = Path(f"/proc/{worker}")
    namespaces = {
        name: {
            "host": str(Path(f"/proc/self/ns/{name}").readlink()),
            "worker": str((root / "ns" / name).readlink()),
        }
        for name in ("user", "mnt", "pid", "net", "ipc", "uts")
    }
    assert all(pair["host"] != pair["worker"] for pair in namespaces.values())
    uid_map = [
        tuple(map(int, line.split())) for line in (root / "uid_map").read_text().splitlines()
    ]
    assert (65534, os.getuid(), 1) in uid_map
    gid_map = [
        tuple(map(int, line.split())) for line in (root / "gid_map").read_text().splitlines()
    ]
    assert (65534, os.getgid(), 1) in gid_map
    status = (root / "status").read_text()
    try:
        apparmor_profile = (root / "attr/current").read_text().strip()
    except OSError:
        apparmor_profile = "unavailable"
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert all(int(fields[name].strip(), 16) == 0 for name in ("CapEff", "CapPrm", "CapBnd"))
    assert fields["NoNewPrivs"].strip() == "1"
    environment = dict(
        item.split(b"=", 1) for item in (root / "environ").read_bytes().split(b"\0") if item
    )
    # bwrap establishes PWD after clearing the inherited environment. This
    # literal sandbox location is permitted; no Host variable may survive.
    assert environment == {b"PWD": b"/tmp"}
    mountinfo = (root / "mountinfo").read_text()
    mounts = [line.split() for line in mountinfo.splitlines()]
    native_mount = next(row for row in mounts if row[4] == "/worker/monty")
    assert "ro" in native_mount[5].split(",")
    assert not any(
        row[4].startswith(("/app", "/home", "/run/docker", "/workspace")) for row in mounts
    )
    for name in ("app/data", "home", "run/docker.sock", "workspace", "app/config", "proc"):
        assert not (root / "root" / name).exists()
    interfaces = [
        line.split(":")[0].strip()
        for line in (root / "net/dev").read_text().splitlines()
        if ":" in line
    ]
    assert interfaces == ["lo"]
    limits = (root / "limits").read_text()
    assert any("Max address space" in line and "536870912" in line for line in limits.splitlines())
    assert any("Max cpu time" in line and "30" in line for line in limits.splitlines())
    assert any("Max core file size" in line and "0" in line for line in limits.splitlines())
    assert any("Max file size" in line and "4194304" in line for line in limits.splitlines())
    assert any("Max open files" in line and "32" in line for line in limits.splitlines())
    return worker, {
        "host_uid": os.getuid(),
        "apparmor_profile": apparmor_profile,
        "procfs_exposed": False,
        "worker_namespace_uid": 65534,
        "namespaces": namespaces,
        "uid_map": uid_map,
        "gid_map": gid_map,
        "environment": {"PWD": "/tmp"},
        "mountinfo": mountinfo,
        "network_interfaces": interfaces,
        "limits": limits,
        "status": status,
        "process_ids": candidates,
        "native_pid": worker,
    }


async def wait_gone(pids: set[int]) -> None:
    for _ in range(150):
        remaining = await asyncio.to_thread(
            lambda: [pid for pid in pids if Path(f"/proc/{pid}").exists()]
        )
        if not remaining:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"worker descendants survived: {remaining}")


async def parent_probe(launcher: Path) -> None:
    """A separate unprivileged binding owner, killed by the validation parent."""
    import pydantic_monty

    async with pydantic_monty.AsyncMonty(
        binary_path=launcher, min_processes=0, max_processes=1, max_checkouts_per_worker=1
    ) as pool:
        async with pool.checkout() as session:
            boundary = await session.feed_start("lookup(1)", skip_type_check=True)
            assert boundary.function_name == "lookup"
            _, facts = observe(session.worker_pid)
            print(json.dumps(facts), flush=True)
            await asyncio.Event().wait()


async def verify_parent_death(launcher: Path) -> dict[str, Any]:
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        "--launcher",
        str(launcher),
        "--parent-probe",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert owner.stdout is not None
        line = await asyncio.wait_for(owner.stdout.readline(), 10)
        if not line:
            _, error = await owner.communicate()
            raise AssertionError(f"binding owner failed before suspension: {error.decode()}")
        facts = json.loads(line)
        pids = set(facts["process_ids"])
        owner.send_signal(signal.SIGKILL)
        await owner.wait()
        await wait_gone(pids)
        return {"owner_signal": "SIGKILL", "process_ids": sorted(pids), "verdict": "passed"}
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.wait()


async def verify_host_policy(worker: Path, launcher: Path) -> dict[str, Any]:
    from qq_ai_bot.codemode.capacity import runtime_capacity
    from qq_ai_bot.codemode.driver_types import EngineAnswer
    from qq_ai_bot.codemode.engine_monty import MontyEngine, PinnedWorker
    from qq_ai_bot.codemode.limits import CodeModeLimits

    pinned = PinnedWorker(worker, digest(worker), launcher, digest(launcher))
    limits = CodeModeLimits(max_feed_seconds=0.1, request_timeout_seconds=0.5)
    capacity = runtime_capacity(2, 1)
    process_ids: set[int] = set()
    memory_kib = []
    async with (
        MontyEngine(pinned, limits, background=True) as background,
        MontyEngine(pinned, replace(limits, request_timeout_seconds=5), background=True) as queued,
        MontyEngine(pinned, limits) as foreground,
    ):
        one, two, urgent = (
            engine.run(frozenset({"lookup"})) for engine in (background, queued, foreground)
        )
        waiting = None
        try:
            assert (await one.start("lookup(1)", {})).status == "suspended"
            waiting = asyncio.create_task(two.start("lookup(2)", {}))
            await asyncio.sleep(0.05)
            assert two._session is None and capacity.active == capacity.background == 1
            assert (await urgent.start("lookup(3)", {})).status == "suspended"
            assert capacity.active == capacity.peak == 2 and capacity.background == 1
            for run in (one, urgent):
                _, facts = observe(run._session.worker_pid)
                process_ids.update(facts["process_ids"])
                memory_kib.append(
                    int(
                        next(
                            line
                            for line in facts["status"].splitlines()
                            if line.startswith("VmHWM:")
                        ).split()[1]
                    )
                )
            waiting.cancel()
            assert isinstance(
                (await asyncio.gather(waiting, return_exceptions=True))[0], asyncio.CancelledError
            )
            assert two._session is None
        finally:
            if waiting is not None and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await asyncio.gather(one.terminate(), two.terminate(), urgent.terminate())
    await wait_gone(process_ids)
    assert capacity.active == capacity.background == 0
    result: dict[str, Any] = {
        "max_native_workers": 2,
        "background_limit": 1,
        "observed_peak_native_workers": capacity.peak,
        "native_high_water_rss_kib": memory_kib,
        "queued_cancellation_created_no_worker": True,
        "all_capacity_process_ids_gone": sorted(process_ids),
        "measurement_scope": "two suspended synthetic scripts; not production capacity sizing",
    }
    # SIGSTOP makes the native process unable to enforce its own feed limit.
    # The real binding's separate Host wall watchdog must kill the full tree.
    async with MontyEngine(pinned, limits) as engine:
        run = engine.run(frozenset({"lookup"}))
        try:
            boundary = await run.start("lookup(1)", {})
            assert boundary.status == "suspended" and boundary.call is not None
            native, facts = observe(run._session.worker_pid)
            stopped_ids = set(facts["process_ids"])
            os.kill(native, signal.SIGSTOP)
            for _ in range(100):
                stopped_status = await asyncio.to_thread(Path(f"/proc/{native}/status").read_text)
                state = next(
                    line for line in stopped_status.splitlines() if line.startswith("State:")
                )
                if "T (stopped)" in state:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("native worker did not enter the stopped state")
            started = time.monotonic()
            outcome = await asyncio.wait_for(
                run.answer(boundary.call.engine_call_id, EngineAnswer.ok(2)), 3
            )
            elapsed = time.monotonic() - started
            assert outcome.status == "failed" and outcome.failure is not None, outcome
            assert outcome.failure.category == "limit_time" and outcome.failure.worker_discarded
            await wait_gone(stopped_ids)
            result["host_watchdog"] = {
                "configured_seconds": limits.request_timeout_seconds,
                "elapsed_seconds": elapsed,
                "failure_category": outcome.failure.category,
                "all_process_ids_gone": sorted(stopped_ids),
            }
        finally:
            await run.terminate()
    assert capacity.active == 0
    return result


async def verify(worker: Path, launcher: Path) -> dict[str, Any]:
    if platform.system() != "Linux" or os.geteuid() == 0:
        raise ValueError("isolation_validation_requires_unprivileged_linux")
    import pydantic_monty

    report: dict[str, Any] = {
        "format": "yuki_monty_linux_isolation_v1",
        "platform": platform.platform(),
        "worker_sha256": digest(worker),
        "launcher_sha256": digest(launcher),
        "bwrap_sha256": digest(Path("/usr/bin/bwrap")),
        "verdict": "not_run",
    }
    process_ids: set[int] = set()
    async with pydantic_monty.AsyncMonty(
        binary_path=launcher,
        min_processes=0,
        max_processes=1,
        max_checkouts_per_worker=1,
        request_timeout=5,
    ) as pool:
        async with pool.checkout(
            limits={"max_feed_duration_secs": 1, "max_memory": 16 << 20}
        ) as session:
            boundary = await session.feed_start("lookup(1)", skip_type_check=True)
            assert boundary.function_name == "lookup"
            pool_pid = session.worker_pid
            _, facts = observe(pool_pid)
            process_ids.update(facts["process_ids"])
            report["worker"] = facts
            complete = await boundary.resume({"return_value": 2})
            assert complete.output == 2
        async with pool.checkout(
            limits={"max_feed_duration_secs": 0.2, "max_memory": 16 << 20}
        ) as session:
            # Start at a real suspension so all timeout-test descendants are
            # observed before the next feed enters the infinite loop.
            boundary = await session.feed_start(
                "lookup(2)\nwhile True:\n    pass", skip_type_check=True
            )
            _, facts = observe(session.worker_pid)
            process_ids.update(facts["process_ids"])
            try:
                await boundary.resume({"return_value": 2})
            except pydantic_monty.MontyRuntimeError as exc:
                assert type(exc.exception()).__name__ == "TimeoutError"
                report["infinite_loop"] = "engine_time_limit"
            else:
                raise AssertionError("infinite loop was not terminated")
    await wait_gone(process_ids)
    report["terminated_process_ids"] = sorted(process_ids)
    report["termination"] = "all_original_process_ids_gone"
    report["parent_death"] = await verify_parent_death(launcher)
    report["host_policy"] = await verify_host_policy(worker, launcher)
    report["verdict"] = "passed"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, default=Path("/opt/yuki-monty/monty"))
    parser.add_argument("--launcher", type=Path, default=Path("/opt/yuki-monty/monty-isolated"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--parent-probe", action="store_true")
    args = parser.parse_args()
    if args.parent_probe:
        asyncio.run(parent_probe(args.launcher))
        return
    if args.output is None:
        parser.error("--output is required for validation")
    result = asyncio.run(verify(args.worker, args.launcher))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("verdict", "worker_sha256", "launcher_sha256", "termination")
            }
        )
    )


if __name__ == "__main__":
    main()
