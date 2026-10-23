"""Isolated burst/ACK transport measurement; no model or production traffic."""

import asyncio
import json
import subprocess
import tempfile
import time
import tracemalloc
import types
from pathlib import Path

from yuki_agent_connector.config import Config
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal


def load(name, path):
    module = types.ModuleType("yuki_agent_connector." + name)
    module.__package__ = "yuki_agent_connector"
    source = subprocess.check_output(
        [
            "git",
            "show",
            "8aef3f0b:packages/yuki-agent-connector/src/yuki_agent_connector/"
            + path.name.removeprefix("baseline_"),
        ],
        text=True,
        encoding="utf-8",
    )
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


async def measure(label, journal_type, connector_type, count, rtt):
    with tempfile.TemporaryDirectory() as directory:
        journal = journal_type(Path(directory) / "journal.db", namespace="performance-fixture")
        command = {"id": "original", "instance_id": "local", "action": "prompt", "arguments": {}}
        journal.begin(command)
        journal.complete(
            "original",
            {
                "result": {
                    "status": "running",
                    "admitted": True,
                    "pending": True,
                    "session_id": "session",
                }
            },
        )
        append_times = []
        for n in range(count):
            start = time.perf_counter()
            journal.append(
                {
                    "type": "event",
                    "event_id": str(n),
                    "instance_id": "local",
                    "kind": "progress",
                    "operation_id": "original",
                    "session_id": "session",
                    "payload": {
                        "status": "running",
                        "admitted": True,
                        "item": {
                            "type": "commandExecution",
                            "command": "fixture",
                            "status": "completed",
                        },
                    },
                }
            )
            append_times.append((time.perf_counter() - start) * 1000)
        connector = connector_type(Config("ws://unused", "fixture", "fixture"), journal)
        done = asyncio.Event()
        acks = []
        pending = set()
        start = time.perf_counter()
        cpu = time.process_time()
        tracemalloc.start()

        class Transport:
            async def send(self, encoded):
                value = json.loads(encoded)

                async def acknowledge():
                    await asyncio.sleep(rtt)
                    journal.ack(value["event_id"])
                    if label == "baseline":
                        connector._event_ack[1].set_result(None)
                    acks.append((time.perf_counter() - start) * 1000)
                    if len(acks) == count:
                        done.set()

                task = asyncio.create_task(acknowledge())
                pending.add(task)
                task.add_done_callback(pending.discard)

        sender = asyncio.create_task(connector._events(Transport()))
        if count:
            await asyncio.wait_for(done.wait(), 60)
        else:
            await asyncio.sleep(0)
        elapsed = time.perf_counter() - start
        cpu = time.process_time() - cpu
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        sender.cancel()
        await asyncio.gather(sender, *pending, return_exceptions=True)
        journal.close()

        def percentile(values, fraction):
            return sorted(values)[int((len(values) - 1) * fraction)] if values else 0

        return {
            "variant": label,
            "events": count,
            "simulated_ack_rtt_ms": rtt * 1000,
            "drain_ms": elapsed * 1000,
            "ack_p50_ms": percentile(acks, 0.5),
            "ack_p95_ms": percentile(acks, 0.95),
            "cpu_ms": cpu * 1000,
            "peak_transport_allocated_bytes": peak,
            "durable_append_p50_ms": percentile(append_times, 0.5),
            "durable_append_p95_ms": percentile(append_times, 0.95),
        }


async def main():
    root = Path(__file__).parent
    old_journal = load("_baseline_journal", root / "baseline_journal.py").Journal
    old_connector = load("_baseline_connector", root / "baseline_connector.py").Connector
    results = []
    for count, rtt in [(0, 0), (1, 0), (100, 0), (10000, 0), (100, 0.05)]:
        for label, journal, connector in [
            ("baseline", old_journal, old_connector),
            ("candidate", Journal, Connector),
        ]:
            result = await measure(label, journal, connector, count, rtt)
            results.append(result)
            print(json.dumps(result), flush=True)
    (root / "results.json").write_text(
        json.dumps(
            {
                "baseline": "8aef3f0b",
                "method": (
                    "Same precommitted burst. In-process send/ACK peer with configured ACK delay, "
                    "real SQLite FULL journal. Allocations are traced Python allocations; "
                    "append duration includes serialization and commit, not server writer hold. "
                    "Real TLS transport covered separately."
                ),
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
