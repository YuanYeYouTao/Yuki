"""Paired local SQLite/real Main load; fake model and in-memory platform only.

Run the same file with each --checkout and the same arguments in alternating
order. Imports must come from that checkout; no network or live Bot is started.
The fixture helpers only create an identical logical deployment before timing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "mean_ms": statistics.mean(ordered) * 1000,
        "p50_ms": ordered[(len(ordered) - 1) // 2] * 1000,
        "p95_ms": ordered[int((len(ordered) - 1) * 0.95)] * 1000,
        "max_ms": ordered[-1] * 1000,
    }


async def main(args: argparse.Namespace) -> dict[str, object]:
    checkout = args.checkout.resolve()
    sys.path[:0] = [str(checkout / "src"), str(checkout)]
    os.chdir(checkout)
    os.environ["SOUL_FILE"] = str(checkout / "config/SOUL.md")
    from sqlalchemy import select
    from tests.integration.test_current_material_main_contract import _deployment
    from tests.unit.test_memory_mutation import _event
    from tests.unit.test_self_initiative_memory import seed

    import yuki.persistence.database as database_module
    from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
    from yuki.llm.fake import FakeLLMProvider
    from yuki.memory.self_reflection.repository import SelfReflectionRepository
    from yuki.memory.self_reflection.service import _CHECKPOINT, ReflectionCheckpoint
    from yuki.persistence.database import Database
    from yuki.persistence.models import ChatEventModel, MemorySelfReflectionRunModel

    source = await asyncio.to_thread(Path(database_module.__file__).resolve)
    assert source.is_relative_to(checkout), source
    args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sw-", dir=args.scratch) as temporary:
        root = Path(temporary)
        database = Database(f"sqlite+aiosqlite:///{root / 'load.db'}")
        await database.create_schema()
        waits: list[float] = []
        scopes: list[float] = []
        immediate = database.immediate_session

        @asynccontextmanager
        async def measured_writer():
            start = time.perf_counter()
            async with immediate() as writer:
                entered = time.perf_counter()
                waits.append(entered - start)
                yield writer
            scopes.append(time.perf_counter() - entered)

        database.immediate_session = measured_writer
        result: dict[str, object] = {
            "checkout": str(checkout),
            "source": str(source),
            "scenario": args.scenario,
        }
        app = None
        try:
            repository = SelfReflectionRepository(database)
            if args.scenario == "tail":
                mutations, _, _, _ = await seed(database)
                await repository.scan_new_events()
                await _event(
                    mutations._ledger,
                    message_id="first",
                    sender_user_id="1001",
                    content="completed range",
                    group_id="3001",
                )
                await repository.scan_new_events()
                (batch,) = await repository.claim_due(
                    scheduled_slot="tail",
                    event_threshold=1,
                    character_threshold=1,
                    max_wait_seconds=0,
                    max_events=1,
                    max_characters=16000,
                )
                for index in range(args.tail):
                    await _event(
                        mutations._ledger,
                        message_id=f"tail-{index}",
                        sender_user_id="1001",
                        content=f"source {index}: " + "conversation material " * 80,
                        group_id="3001",
                    )
                await repository.scan_new_events()
                async with immediate() as writer:
                    run = await writer.get(MemorySelfReflectionRunModel, batch.run_id)
                    run.checkpoint_json = '{"completed_counts":[0,0]}'
                waits.clear()
                scopes.clear()
                started = time.perf_counter()
                recovered = await repository.recover_stale_runs(started_before=datetime.now(UTC))
                elapsed = time.perf_counter() - started
                assert recovered == 1
                result.update(tail_events=args.tail, recovery_ms=elapsed * 1000)
            else:

                def respond(request):
                    if request.messages[-1].role == "tool":
                        return "NO_REPLY"
                    return ChatResponse(
                        content="",
                        latency_seconds=0,
                        tool_calls=(
                            ToolCall(
                                "fixed-send",
                                ToolFunction("send", '{"text":"local benchmark reply"}'),
                            ),
                        ),
                    )

                app, _environment, message, sender = await _deployment(
                    database,
                    root,
                    FakeLLMProvider(respond),
                    per_user_requests_per_minute=100000,
                    per_group_requests_per_minute=100000,
                    global_llm_concurrency=16,
                )
                await app._freeze_main_manifest()
                await repository.scan_new_events()
                # Warm the same real Main entry before measuring arrivals.
                await app.processor.handle(replace(message, message_id="warmup"), sender)
                waits.clear()
                scopes.clear()
                latencies: list[float] = []
                completions: list[float] = []
                background_latencies: list[float] = []
                started = time.perf_counter()

                async def chat(index):
                    due = started + index * args.interval
                    await asyncio.sleep(max(0, due - time.perf_counter()))
                    begin = time.perf_counter()
                    await app.processor.handle(
                        replace(message, message_id=f"measured-{index}"), sender
                    )
                    end = time.perf_counter()
                    latencies.append(end - begin)
                    completions.append(end - due)

                async def reflection(index):
                    due = started + index * args.background_interval
                    await asyncio.sleep(max(0, due - time.perf_counter()))
                    begin = time.perf_counter()
                    await repository.scan_new_events()
                    batches = await repository.claim_due(
                        scheduled_slot=f"load-{index}",
                        event_threshold=1,
                        character_threshold=1,
                        max_wait_seconds=0,
                        max_events=100,
                        max_characters=16000,
                    )
                    for batch in batches:
                        raw = _CHECKPOINT.dump_json(
                            ReflectionCheckpoint(
                                {"proposals": [], "episodes": []},
                                {f"event_{n}": event for n, event in enumerate(batch.events, 1)},
                                {},
                            )
                        ).decode()
                        await repository.save_checkpoint(batch.run_id, raw)
                        await repository.complete(batch, proposals=0, committed=0)
                    background_latencies.append(time.perf_counter() - begin)

                jobs = [chat(index) for index in range(args.messages)]
                if args.scenario == "mixed":
                    jobs += [reflection(index) for index in range(args.background)]
                await asyncio.gather(*jobs)
                elapsed = time.perf_counter() - started
                # Verify original inbound IDs, successful Social send facts, and model count.
                reader_factory = (
                    database.read_session
                    if hasattr(database, "read_session")
                    else database.sessions
                )
                async with reader_factory() as reader:
                    incoming = tuple(
                        await reader.scalars(
                            select(ChatEventModel.platform_message_id).where(
                                ChatEventModel.platform_message_id.like("measured-%"),
                                ChatEventModel.direction == "inbound",
                            )
                        )
                    )
                    sent = tuple(
                        await reader.scalars(
                            select(ChatEventModel.id).where(
                                ChatEventModel.content == "local benchmark reply",
                                ChatEventModel.direction == "outbound",
                            )
                        )
                    )
                assert len(incoming) == args.messages, incoming
                assert len(sent) == args.messages + 1, (len(sent), args.messages + 1)
                result.update(
                    messages=args.messages,
                    background_jobs=len(background_latencies),
                    interval_seconds=args.interval,
                    elapsed_seconds=elapsed,
                    messages_per_second=args.messages / elapsed,
                    handler=distribution(latencies),
                    arrival_to_exit=distribution(completions),
                    background=distribution(background_latencies),
                    verified_inbound=len(incoming),
                    verified_sends=len(sent),
                )
            result.update(
                writer_entry_wait=distribution(waits),
                writer_scope_and_exit=distribution(scopes),
                phase_semantics=(
                    "entry_wait includes checkout/BEGIN; scope_and_exit starts after BEGIN "
                    "and includes physical commit/cleanup; explicit immediate scopes only, "
                    "baseline mixed sessions are excluded from these two distributions"
                ),
            )
            return result
        finally:
            if app is not None:
                errors = await app.lifecycle._close_entries(app.lifecycle._entries)
                assert not errors, errors
            await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scratch", type=Path, default=Path("D:/Temp/yuki-sqlite-perf"))
    parser.add_argument("--scenario", choices=("chat", "mixed", "tail"), required=True)
    parser.add_argument("--messages", type=int, default=12)
    parser.add_argument("--interval", type=float, default=0.15)
    parser.add_argument("--background", type=int, default=8)
    parser.add_argument("--background-interval", type=float, default=0.2)
    parser.add_argument("--tail", type=int, default=512)
    arguments = parser.parse_args()
    report = asyncio.run(main(arguments))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
