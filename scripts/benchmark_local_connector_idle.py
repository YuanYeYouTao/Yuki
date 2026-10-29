"""Isolated Connector idle sample; no user server, daemon, browser or model is used."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

import trustme
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

REPOSITORY = Path(__file__).resolve().parents[1]


def process_sample(pid: int) -> dict[str, float | int]:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working_set",
                    "working_set",
                    "peak_paged_pool",
                    "paged_pool",
                    "peak_nonpaged_pool",
                    "nonpaged_pool",
                    "pagefile",
                    "peak_pagefile",
                    "private_bytes",
                )
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_operations",
                    "write_operations",
                    "other_operations",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO)]
        memory_api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        memory_api.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            memory, io = Memory(), IO()
            memory.cb = ctypes.sizeof(memory)
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(system),
                ctypes.byref(user),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not memory_api(handle, ctypes.byref(memory), ctypes.sizeof(memory)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.GetProcessIoCounters(handle, ctypes.byref(io)):
                raise ctypes.WinError(ctypes.get_last_error())
            cpu = (
                sum((value.dwHighDateTime << 32) + value.dwLowDateTime for value in (system, user))
                / 10000000
            )
            return {
                "cpu_seconds": cpu,
                "rss_bytes": memory.working_set,
                "private_bytes": memory.private_bytes,
                "read_bytes": io.read_bytes,
                "write_bytes": io.write_bytes,
            }
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        memory = Path(f"/proc/{pid}/statm").read_text().split()
        return {
            "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            "rss_bytes": int(memory[1]) * os.sysconf("SC_PAGE_SIZE"),
        }
    raise RuntimeError("This probe currently measures Windows and Linux processes")


async def sample(output: Path, seconds: int, connector_source: Path, full_access: bool) -> None:
    ready = asyncio.Event()
    counters = {"received_application_bytes": 0, "sent_application_bytes": 0}

    async def peer(socket):
        try:
            async for raw in socket:
                counters["received_application_bytes"] += len(
                    raw.encode() if isinstance(raw, str) else raw
                )
                message = json.loads(raw)
                if message["type"] == "hello":
                    assert message["instances"] == []
                    ready.set()
        except ConnectionClosed:
            pass  # This fixture closes its original TLS connection after sampling.

    with tempfile.TemporaryDirectory(prefix="yuki-connector-idle-") as temporary:
        root = Path(temporary)
        ca = trustme.CA()
        cert = ca.issue_cert("localhost", "127.0.0.1")
        ca.cert_pem.write_to_path(root / "ca.pem")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert.configure_cert(tls)
        async with serve(peer, "127.0.0.1", 0, ssl=tls) as server:
            port = server.sockets[0].getsockname()[1]
            data = root / "connector"
            data.mkdir()
            # A retained 11 MiB log fixture checks that idle does not rescan it.
            for suffix, size in (("", 3), (".1", 4), (".2", 4)):
                (data / f"connector.log{suffix}").write_bytes(b"x" * size * 1024 * 1024)
            config = data / "config.json"
            settings = {
                "url": f"wss://127.0.0.1:{port}",
                "device_id": "isolated-idle-fixture",
                "token": "fixture-only",
                "instances": [],
            }
            if full_access:
                settings["full_access"] = True
            config.write_text(
                json.dumps(settings),
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            }
            environment["SSL_CERT_FILE"] = str(root / "ca.pem")
            environment["PYTHONPATH"] = os.pathsep.join(
                (
                    str(connector_source),
                    sysconfig.get_path("purelib"),
                )
            )
            # Windows venv python.exe is a redirector; measure the actual Python
            # interpreter, using the identical installed development libraries.
            executable = sys._base_executable
            process = await asyncio.create_subprocess_exec(
                executable,
                "-m",
                "yuki_agent_connector.cli",
                "--data-dir",
                str(data),
                "--config",
                str(config),
                "run",
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(ready.wait(), 10)
                await asyncio.sleep(1)
                started = time.perf_counter()
                samples = []
                for index in range(seconds + 1):
                    if index:
                        await asyncio.sleep(1)
                    values = process_sample(process.pid)
                    samples.append(
                        {
                            "elapsed_seconds": time.perf_counter() - started,
                            **values,
                            **counters,
                            "log_bytes": sum(
                                path.stat().st_size for path in data.glob("connector.log*")
                            ),
                        }
                    )
                first, last = samples[0], samples[-1]
                elapsed = last["elapsed_seconds"] - first["elapsed_seconds"]
                report = {
                    "workload": (
                        "one isolated CLI Connector, TLS loopback fixture, instances=[], "
                        "no commands/browser/models; 11 MiB retained logs"
                    ),
                    "platform": sys.platform,
                    "python": sys.version,
                    "executable": executable,
                    "connector_source": str(connector_source),
                    "full_access_configured": full_access,
                    "pid": process.pid,
                    "measurement": (
                        "after hello and 1s warmup; 1s interval; process CPU/kernel+user, "
                        "working-set RSS, OS process IO, WebSocket application payload"
                    ),
                    "duration_seconds": elapsed,
                    "cpu_seconds_delta": last["cpu_seconds"] - first["cpu_seconds"],
                    "cpu_one_core_percent": 100
                    * (last["cpu_seconds"] - first["cpu_seconds"])
                    / elapsed,
                    "rss_min_bytes": min(item["rss_bytes"] for item in samples),
                    "rss_max_bytes": max(item["rss_bytes"] for item in samples),
                    "application_receive_delta_bytes": last["received_application_bytes"]
                    - first["received_application_bytes"],
                    "application_send_delta_bytes": last["sent_application_bytes"]
                    - first["sent_application_bytes"],
                    "log_growth_bytes": last["log_bytes"] - first["log_bytes"],
                    "caveats": [
                        "single local development runtime sample, not SLA or production traffic",
                        "15s sample does not cover the default 20s WebSocket ping period",
                        "application bytes exclude transport framing and ping control frames",
                        "no native Agent, browser, output backlog, reconnect or active shell",
                        "CPU quantization can hide small activity; RSS is process working set",
                        "seeded retained logs test idle growth, not rotation under load",
                    ],
                    "raw_samples": samples,
                }
                output.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    output.write_text,
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    json.dumps(
                        {key: value for key, value in report.items() if key != "raw_samples"},
                        ensure_ascii=False,
                    )
                )
            finally:
                # This isolated process has no native owners or issued jobs.
                # Terminate the exact interpreter we started after sampling.
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument(
        "--connector-source", type=Path, default=REPOSITORY / "packages/yuki-agent-connector/src"
    )
    parser.add_argument("--full-access", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(
        sample(
            arguments.output,
            arguments.seconds,
            arguments.connector_source.resolve(),
            arguments.full_access,
        )
    )
