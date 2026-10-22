"""Temporary Linux cgroup-v2 wrapper; the benchmark source is unchanged.

Kernel interface: https://docs.kernel.org/admin-guide/cgroup-v2.html
Resource/process counters: https://docs.python.org/3.12/library/resource.html
https://docs.kernel.org/filesystems/proc.html
Only a fresh leaf is configured. No parent controller or global limit is changed.
"""

import argparse
import hashlib
import json
import os
import pwd
import resource
import runpy
import subprocess
import sys
import time
import uuid
from pathlib import Path


def usage(value):
    return {name: getattr(value, name) for name in dir(value) if name.startswith("ru_")}


def process_io():
    return {
        key: int(value)
        for key, value in (
            line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines()
        )
    }


def child():
    metrics, script, *arguments = sys.argv[2:]
    sys.argv = [script, *arguments]
    sys.path.insert(0, str(Path(script).parent))
    initial = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "cgroup": Path("/proc/self/cgroup").read_text().strip(),
        "affinity_before_script": sorted(os.sched_getaffinity(0)),
    }
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        initial.update(
            affinity_after_script=sorted(os.sched_getaffinity(0)),
            rusage_self=usage(resource.getrusage(resource.RUSAGE_SELF)),
            rusage_waited_children=usage(resource.getrusage(resource.RUSAGE_CHILDREN)),
            proc_io_self=process_io(),
            proc_status_self=Path("/proc/self/status").read_text(),
            semantics=(
                "Linux ru_maxrss is KiB. These counters cover the entire script process, "
                "including imports, fixtures and output, not only the benchmark timed region. "
                "proc_io is the main process; cgroup counters include its descendants."
            ),
        )
        Path(metrics).write_text(json.dumps(initial, indent=2) + "\n")


def snapshot(group):
    return {
        name: (group / name).read_text().strip()
        for name in (
            "memory.max",
            "memory.swap.max",
            "memory.oom.group",
            "memory.current",
            "memory.peak",
            "memory.events",
            "memory.stat",
            "memory.swap.current",
            "cpu.stat",
            "io.stat",
            "cgroup.procs",
            "cgroup.events",
        )
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--user", required=True, help="Existing unprivileged Linux account")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if len(command) < 2:
        parser.error("command must be Python executable, script and its original arguments")
    if os.geteuid() != 0:
        parser.error("fresh cgroup leaf setup requires root; child drops to --user")
    account = pwd.getpwnam(args.user)
    args.sidecar.parent.mkdir(parents=True, exist_ok=True)
    child_metrics = args.sidecar.with_suffix(".process.json")
    child_metrics.unlink(missing_ok=True)
    stdout = args.sidecar.with_suffix(".stdout.txt")
    stderr = args.sidecar.with_suffix(".stderr.txt")
    group = Path("/sys/fs/cgroup") / ("yuki-sqlite-cap-" + uuid.uuid4().hex)
    report = {
        "command": command,
        "memory_limit_bytes": args.memory_mib * 1024 * 1024,
        "swap_limit_bytes": 0,
        "user": args.user,
        "uid": account.pw_uid,
        "cgroup": str(group),
        "kernel": os.uname().release,
        "script_sha256": hashlib.sha256(Path(command[1]).read_bytes()).hexdigest(),
        "stdout": str(stdout),
        "stderr": str(stderr),
        "references": [
            "https://docs.kernel.org/admin-guide/cgroup-v2.html",
            "https://docs.kernel.org/filesystems/proc.html",
            "https://docs.python.org/3.12/library/resource.html",
        ],
        "semantics": (
            "memory.max is a real memory-cgroup limit, not RLIMIT_AS or an RSS promise. "
            "memory.peak includes charged memory/cache/kernel pages for the leaf and descendants. "
            "io.stat is kernel-accounted device I/O; empty remains empty, not fabricated activity. "
            "All sidecar resource counters cover the whole child lifetime. "
            "Original benchmark JSON remains the timed workload evidence."
        ),
    }
    try:
        group.mkdir()
        for name, value in (
            ("memory.max", report["memory_limit_bytes"]),
            ("memory.swap.max", 0),
            ("memory.oom.group", 1),
        ):
            (group / name).write_text(str(value))
        report["before"] = snapshot(group)

        def enter():
            (group / "cgroup.procs").write_text(str(os.getpid()))
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)

        environment = {**os.environ, "HOME": account.pw_dir, "USER": args.user}
        actual = [
            command[0],
            str(Path(__file__).resolve()),
            "--child",
            str(child_metrics),
            *command[1:],
        ]
        start = time.monotonic()
        with stdout.open("wb") as output, stderr.open("wb") as error:
            process = subprocess.Popen(
                actual, stdout=output, stderr=error, env=environment, preexec_fn=enter
            )
            _, status, observed = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
        report.update(
            exit_code=process.returncode,
            lifetime_seconds=time.monotonic() - start,
            wait4_rusage=usage(observed),
            after=snapshot(group),
            process=json.loads(child_metrics.read_text()) if child_metrics.exists() else None,
        )
        return process.returncode
    except BaseException as error:
        report["runner_error"] = repr(error)
        raise
    finally:
        # Do not kill or silently move leftovers. A nonempty group is a failed
        # owner cleanup and must remain identifiable in the sidecar.
        if group.exists():
            report["final_cgroup_procs"] = (group / "cgroup.procs").read_text().strip()
            if not report["final_cgroup_procs"]:
                group.rmdir()
        report["cgroup_removed"] = not group.exists()
        args.sidecar.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child()
    else:
        raise SystemExit(main())
