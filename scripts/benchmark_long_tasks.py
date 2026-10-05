"""Opt-in, model-planned long tasks: historical/current iteration x direct/code.

Real paid requests require --authorize-paid. Business operations are restricted
to newly generated temporary files, using FileWorkspace and the real journal,
InvocationService and Monty worker. No canned plans are supplied to the model.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts.benchmark_pi_codemode import BASELINE, historical_runner
from scripts.verify_deepseek_codemode import read_credentials

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 8192
RECORDS: list[dict[str, Any]] = []
CREDENTIALS: dict[str, str] = {}
OUTPUT: Path | None = None
STARTED = ""
IN_PROGRESS: dict[str, Any] | None = None
LEDGER: BudgetLedger | None = None
TASKS = ("batch_ledger", "dependency_chain", "resumed_work")


def usage_cost(usage: dict[str, Any]) -> tuple[float, dict[str, int]]:
    """Peak Flash prices; Anthropic input_tokens excludes cache tokens."""
    if "prompt_tokens" in usage:
        total = usage["prompt_tokens"]
        cached = usage.get(
            "prompt_cache_hit_tokens",
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        )
        output = usage["completion_tokens"]
    elif "cache_read_input_tokens" in usage:
        cached = usage["cache_read_input_tokens"]
        total = usage["input_tokens"] + usage.get("cache_creation_input_tokens", 0) + cached
        output = usage["output_tokens"]
    else:
        total = usage["input_tokens"]
        cached = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
        output = usage["output_tokens"]
    if any(type(value) is not int or value < 0 for value in (total, cached, output)):
        raise ValueError("invalid wire usage")
    if cached > total:
        raise ValueError("cache usage exceeds total input")
    return (cached * 0.006 + (total - cached) * 0.30 + output * 1.20) / 1e6, {
        "input": total,
        "cached": cached,
        "output": output,
    }


@dataclass
class BudgetLedger:
    """Reserve before dispatch; release unused reservation only with valid usage.

    Unknown/failed responses retain their full reservation. Prior original
    reports remain immutable; their wire usage settles the previous reservation.
    """

    prior_usd: float = 0.0
    ceiling_usd: float | None = 1.0
    charged_usd: float = 0.0
    physical_calls: int = 0
    maximum_calls: int | None = 400
    pending: dict[int, float] = field(default_factory=dict)
    prior_reports: list[dict[str, Any]] = field(default_factory=list)

    def admit(self, byte_count: int, output_limit: int) -> tuple[int, float]:
        if not byte_count > 0 or not 1 <= output_limit <= MAX_OUTPUT:
            raise RuntimeError("paid request size/output limit reached")
        reserve = (byte_count * 0.30 + output_limit * 1.20) / 1e6
        exposure = self.prior_usd + self.charged_usd + sum(self.pending.values()) + reserve
        if (self.ceiling_usd is not None and exposure > self.ceiling_usd) or (
            self.maximum_calls is not None and self.physical_calls >= self.maximum_calls
        ):
            raise RuntimeError("cumulative paid ceiling reached")
        self.physical_calls += 1
        self.pending[self.physical_calls] = reserve
        return self.physical_calls, reserve

    def settle(self, identity: int, usage: dict[str, Any]) -> tuple[float, dict[str, int]]:
        cost, tokens = usage_cost(usage)
        reserve = self.pending[identity]
        if cost > reserve and self.ceiling_usd is not None:
            # Retain actual exposure and stop; never refund an invalid estimate.
            self.pending[identity] = cost
            raise RuntimeError("wire usage exceeded conservative reservation")
        self.pending.pop(identity)
        self.charged_usd += cost
        return cost, tokens

    def summary(self) -> dict[str, Any]:
        return {
            "cumulative_ceiling_usd": self.ceiling_usd,
            "prior_peak_usage_cost_usd": self.prior_usd,
            "new_peak_usage_cost_usd": self.charged_usd,
            "unsettled_reserved_usd": sum(self.pending.values()),
            "cumulative_peak_exposure_usd": self.prior_usd
            + self.charged_usd
            + sum(self.pending.values()),
            "new_physical_calls": self.physical_calls,
            "prior_reports": self.prior_reports,
            "actual_invoice_verified": False,
            "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
            "peak_usd_per_million": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
        }


def prior_ledger(paths: list[Path], *, unlimited: bool = False) -> BudgetLedger:
    ledger = BudgetLedger(
        ceiling_usd=None if unlimited else 1.0, maximum_calls=None if unlimited else 400
    )
    seen: set[Path] = set()
    for path in paths:
        if path.resolve() in seen:
            raise ValueError("duplicate prior report")
        seen.add(path.resolve())
        raw = path.read_bytes()
        report = json.loads(raw)
        rows = list(report["records"])
        if report.get("in_progress"):
            rows.append(report["in_progress"])
        wires = [wire for row in rows for wire in row["wire"]]
        cost = 0.0
        unknown = 0
        for wire in wires:
            try:
                cost += usage_cost(wire["usage"])[0]
            except (KeyError, TypeError, ValueError):
                cost += (wire["bytes"] * 0.30 + wire["output_limit"] * 1.20) / 1e6
                unknown += 1
        ledger.prior_usd += cost
        ledger.prior_reports.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "calls": len(wires),
                "peak_usd": cost,
                "unknown_usage": unknown,
            }
        )
    if ledger.ceiling_usd is not None and ledger.prior_usd >= ledger.ceiling_usd:
        raise ValueError("prior usage already exhausted cumulative ceiling")
    return ledger


@dataclass(frozen=True)
class Task:
    name: str
    files: dict[str, str]
    expected: dict[str, str]
    instruction: str
    segment_tools: int
    minimum_reads: int
    required_reads: tuple[str, ...]

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.files, sort_keys=True).encode()).hexdigest()


def make_task(name: str, repeat: int) -> Task:
    """Independent CPython oracle; never exposed in the model's prompt."""
    rng = random.Random(3107 + repeat)
    if name == "batch_ledger":
        files, unique = {}, {}
        for sheet in range(24):
            rows = ["id,region,amount,status"]
            for row in range(20):
                identity = (sheet * 17 + row * 7 + repeat * 19) % 251
                region = "ABCD"[identity % 4]
                amount = (identity * 37 + 11 + repeat * 13) % 1000 + 1
                status = "void" if identity % 9 == 0 else "ok"
                rows.append(f"{identity},{region},{amount},{status}")
                unique[identity] = (region, amount, status)
            files[f"ledger/{sheet:02d}.csv"] = "\n".join(rows) + "\n"
        paths = list(files)
        rng.shuffle(paths)
        files["manifest.txt"] = "\n".join(paths) + "\n"
        totals = {region: [0, 0] for region in "ABCD"}
        for region, amount, status in unique.values():
            if status == "ok":
                totals[region][0] += 1
                totals[region][1] += amount
        expected = "\n".join(f"{k}\t{v[0]}\t{v[1]}" for k, v in totals.items()) + "\n"
        instruction = (
            "Read manifest.txt, then every listed CSV file (24 files, 480 records). "
            "Deduplicate records by id across all files (duplicates are identical). Exclude void "
            "records. For each region A,B,C,D compute the unique ok record count and sum amount. "
            "Create batch.tsv, exactly four lines ordered A,B,C,D: region<TAB>count<TAB>sum, "
            "without a header. Verify the saved file."
        )
        return Task(name, files, {"batch.tsv": expected}, instruction, 80, 25, tuple(files))
    if name not in {"dependency_chain", "resumed_work"}:
        raise ValueError("unknown long task")
    depth = 18 if name == "dependency_chain" else 12
    paths = [f"nodes/{rng.randrange(1 << 32):08x}.txt" for _ in range(depth)]
    files, values = {}, []
    for index, path in enumerate(paths):
        value = rng.randrange(10, 100)
        values.append(value)
        successor = paths[index + 1] if index + 1 < depth else "END"
        decoy = f"nodes/decoy-{index}.txt"
        left, right = (successor, decoy) if value % 2 == 0 else (decoy, successor)
        files[path] = f"value={value}\nleft={left}\nright={right}\n"
        files[decoy] = "value=9999\nleft=END\nright=END\n"
    files["start.txt"] = paths[0] + "\n"
    expected = {
        "chain.tsv": "\n".join(f"{p}\t{v}" for p, v in zip(paths, values, strict=True))
        + f"\nTOTAL\t{sum(values)}\n"
    }
    audit = name == "resumed_work"
    if audit:
        expected.update({f"audit/{i:02d}.txt": str(v) + "\n" for i, v in enumerate(values)})
    instruction = (
        "Read start.txt for the first node path. Each node has value,left,right key=value lines. "
        "Choose left when value is even, otherwise right, and follow until END. The unchosen "
        "path is a decoy. Create chain.tsv with one path<TAB>value line for each visited node "
        "in visit order, then TOTAL<TAB>sum. No header. Verify the saved report. "
        + (
            "After reading each chosen node, also create audit/00.txt, audit/01.txt, etc. "
            "in visit order, each containing that node value and a newline. Each audit file "
            "must be written exactly once. The host segments work after five business calls; "
            "continue the original Work when it resumes."
            if audit
            else ""
        )
    )
    return Task(
        name, files, expected, instruction, 5 if audit else 80, depth + 1, ("start.txt", *paths)
    )


def write_report() -> None:
    if OUTPUT is None or LEDGER is None:
        return
    report = {
        "started_at": STARTED,
        "updated_at": datetime.now(UTC).isoformat(),
        "model": CREDENTIALS.get("model"),
        "baseline_commit": BASELINE,
        "baseline_scope": "historic main iteration unchanged; common current invocation/code "
        "helpers, not an unmodified deployed old application",
        "protocol": "chat_completions",
        "fixed_declarations": 76,
        "max_output_tokens": MAX_OUTPUT,
        "reasoning_effort": "low",
        "completion_definition": "independent oracle correct, all required inputs read, report "
        "verified, no repeated committed writes, and original Work durably completed",
        "stop_policy": "runtime pause/failure, or 10 consecutive activations with no new "
        "successful read/write path or context note; no task time ceiling",
        "business_scope": "temporary FileWorkspace, real SQLite/InvocationService/Monty, "
        "no production, gateway or real messaging",
        "recovery_scope": "fresh Runner and WorkControl activations in same process; "
        "not an OS process crash or provider disconnect",
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "records": RECORDS,
        "in_progress": IN_PROGRESS,
        "budget": LEDGER.summary(),
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


async def compare_case(
    database: Any, tmp_path: Path, loop: str, mode: str, task_name: str, repeat: int
) -> None:
    from sqlalchemy import update
    from sqlalchemy.dialects.sqlite import insert
    from tests.integration.test_codemode_runner import runner_env
    from tests.support.agent_backend import StubAgentBackend

    from qq_ai_bot.codemode.api_projection import project
    from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ReasoningEffort
    from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
    from qq_ai_bot.model_runtime.executor import TaskModelExecutor
    from qq_ai_bot.model_runtime.models import (
        ModelCapability,
        ModelProfile,
        ModelProtocol,
        ModelRoute,
        ModelTask,
    )
    from qq_ai_bot.model_runtime.pool import ModelClientPool
    from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
    from qq_ai_bot.model_runtime.routes import ModelRouter
    from qq_ai_bot.runtime.work_budget_schema import budgets
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_supervisor import settle
    from qq_ai_bot.workspace.files import FileWorkspace
    from qq_ai_bot.workspace.store import WorkspaceError

    global IN_PROGRESS
    assert LEDGER is not None
    task = make_task(task_name, repeat)
    workspace_path = (tmp_path / "synthetic-workspace").resolve()
    workspace_path.mkdir()
    files = FileWorkspace(workspace_path)
    for path, text in task.files.items():
        files.write(path, text.encode())
    inventory = json.loads(
        (ROOT / "docs/architecture/pi-codemode-capability-inventory.json").read_text()
    )
    definitions = tuple(ChatTool(**row) for row in inventory["frozen_definitions"])
    revision = inventory["manifest_revision"]
    chat, _, control, runtime, repo = await runner_env(database, tmp_path, iter(()))
    original_runner = chat.runtime.runner
    runner_kind = type(original_runner)
    historic_sha = None
    if loop == "old":
        runner_kind, historic_sha = historical_runner()
    contract = SimpleNamespace(revision=revision, script_api=project(definitions, revision))
    accepted = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": task.instruction,
                "output_kind": "state_change",
                "reporting": "quiet",
                "deliver_artifacts": False,
            },
            "host-accept",
        )
    )
    assert accepted.get("ok")
    assert await repo.renew(control.lease, seconds=300)
    async with database.sessions() as session, session.begin():
        await session.execute(
            insert(budgets)
            .values(root_id=control.current["id"], models=0, tools=0)
            .on_conflict_do_nothing(index_elements=[budgets.c.root_id])
        )
        await session.execute(
            update(budgets)
            .where(budgets.c.root_id == control.current["id"])
            .values(model_limit=None, tool_limit=None)
        )
    wires, turns, segments, business = [], [], [], []
    IN_PROGRESS = {
        "loop": loop,
        "mode": mode,
        "task": task_name,
        "repeat": repeat,
        "wire": wires,
        "model_turns": turns,
        "segments": segments,
        "business_log": business,
    }
    started = time.perf_counter()
    first_correct = None

    class Downstream(StubAgentBackend):
        def definitions(self, runtime, **kwargs):
            return definitions

        def parallel_safe(self, name, runtime):
            return name in {"workspace_read", "workspace_list"}

        def is_side_effecting(self, name, arguments, runtime):
            return name not in {"workspace_read", "workspace_list"}

        async def execute_call(self, invocation):
            nonlocal first_correct
            name = invocation.call.function.name
            args = json.loads(invocation.call.function.arguments)
            item = {
                "name": name,
                "arguments": args,
                "operation_id": invocation.identity.operation_id,
                "elapsed_seconds": time.perf_counter() - started,
            }
            business.append(item)
            try:
                if name == "workspace_read":
                    data = files.read(args["path"], offset=args.get("offset", 0))
                elif name == "workspace_list":
                    data = files.listing(
                        args.get("path", ""),
                        cursor=args.get("cursor", ""),
                        limit=args.get("limit", 50),
                    )
                elif name == "workspace_write":
                    path = args["path"].removeprefix("/workspace/")
                    if path not in task.expected:
                        raise WorkspaceError("benchmark_output_path_denied")
                    data = files.write(path, args["text"].encode(), args.get("expected_version"))
                    if (
                        args["text"].strip() == task.expected[path].strip()
                        and first_correct is None
                    ):
                        first_correct = time.perf_counter() - started
                else:
                    raise WorkspaceError("isolated_capability_denied")
                item["ok"] = True
                return json.dumps({"ok": True, "data": data})
            except (WorkspaceError, OSError, KeyError) as exc:
                item["ok"] = False
                item["error"] = str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
                return json.dumps({"ok": False, "executed": False, "error": item["error"]})

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            return "BENCHMARK_BUDGET_EXHAUSTED"

    async def request_hook(request):
        payload = json.loads(request.content)
        identity, reserve = LEDGER.admit(len(request.content), payload.get("max_tokens", 0))
        request.extensions["benchmark_ledger_id"] = identity
        tools = json.dumps(payload.get("tools", []), sort_keys=True).encode()
        # Only synthetic tool receipts are retained, never raw reasoning or headers.
        errors = []
        for message in payload.get("messages", []):
            if message.get("role") == "tool":
                try:
                    receipt = json.loads(message.get("content", ""))
                    if isinstance(receipt, dict) and (
                        receipt.get("error") or not receipt.get("ok", True)
                    ):
                        errors.append(receipt)
                except (ValueError, TypeError):
                    pass
        wires.append(
            {
                "ledger_id": identity,
                "request_bytes": len(request.content),
                "bytes": len(request.content),
                "output_limit": payload.get("max_tokens", 0),
                "reserved_usd": reserve,
                "tools_sha256": hashlib.sha256(tools).hexdigest(),
                "tools_count": len(payload.get("tools", [])),
                "receipt_errors": errors,
                "started_seconds": time.perf_counter() - started,
            }
        )
        write_report()

    async def response_hook(response):
        await response.aread()
        identity = response.request.extensions["benchmark_ledger_id"]
        wire = next(row for row in wires if row["ledger_id"] == identity)
        wire["status_code"] = response.status_code
        try:
            body = response.json()
            wire["served_model"] = body.get("model")
            wire["finish_reason"] = body.get("choices", [{}])[0].get("finish_reason")
            wire["usage"] = body.get("usage")
            if wire["usage"] is not None:
                cost, tokens = LEDGER.settle(identity, wire["usage"])
                wire.update(estimated_peak_usd=cost, tokens=tokens)
        except (ValueError, KeyError, TypeError):
            wire["usage_unknown"] = True
        wire["latency_seconds"] = time.perf_counter() - started - wire["started_seconds"]
        write_report()

    class Observed(OpenAICompatibleProvider):
        async def complete(self, request):
            result = await super().complete(request)
            turns.append(
                {
                    "status": result.status.value,
                    "tool_calls": [
                        {"name": c.function.name, "arguments": json.loads(c.function.arguments)}
                        for c in result.tool_calls
                    ],
                    "final_text": result.content if not result.tool_calls else None,
                }
            )
            return result

    base = CREDENTIALS["base_url (openai)"].rstrip("/") + "/"
    mode_instruction = (
        "Use direct workspace_read/workspace_write/workspace_list calls only; never execute_code. "
        "You may batch independent direct calls in one response."
        if mode == "direct"
        else "Code Mode is enabled: execute_code is available for loops and aggregation. "
        "Choose your own approach and program; direct workspace calls are also allowed. "
        "Use await yuki_workspace_read/write with the same schemas and receipt data.text."
    )
    instruction = (
        "This is an isolated long-task benchmark. Work already accepted. Only temporary "
        "workspace_read/workspace_write/workspace_list are authorized; never send messages, "
        "use memory, terminal, network or other business tools. "
        "Task_control lifecycle is available. "
        "You may save a progress context_note with task_control(update) before a segment ends. "
        "Receipts contain data.text on reads and data.path/version on writes. CSV fields "
        "contain no quotes or commas; node files are key=value text. In Code Mode use "
        "string methods and Python builtins; module imports except asyncio are unavailable. "
        "All input files are under /workspace; outputs are new files. When verified, return "
        "TASK_DONE. First call task_control(action='complete') in its own tool batch after "
        "verifying all requested files. Keep your progress in context_note when needed. "
        "Do not claim success before completing the requested files. " + mode_instruction
    )
    initial = (ChatMessage("system", instruction), ChatMessage("user", task.instruction))
    error = None
    stopped_for_stall = False
    failure_details: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=base,
        timeout=600,
        event_hooks={"request": [request_hook], "response": [response_hook]},
    ) as client:
        adapter = Observed(
            base_url=base,
            api_key=CREDENTIALS["api_key"],
            timeout_seconds=600,
            max_retries=0,
            client=client,
            provider_name="deepseek",
        )
        profile = ModelProfile(
            id="long-task-benchmark",
            provider="deepseek",
            protocol=ModelProtocol.CHAT_COMPLETIONS,
            base_url=base,
            api_key_env="UNUSED",
            model=CREDENTIALS["model"],
            timeout_seconds=600,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=MAX_OUTPUT,
            reasoning_effort=ReasoningEffort.LOW,
            capabilities={ModelCapability.TOOLS, ModelCapability.REASONING},
        )
        executor = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=ModelClientPool(injected_profiles={profile.id: adapter}),
        )
        backend = Downstream()
        config = replace(
            runtime.runtime_config,
            llm=replace(
                runtime.runtime_config.llm, max_output_tokens=MAX_OUTPUT, thinking_enabled=True
            ),
        )
        result = None
        index, stagnant = 0, 0
        observed_progress: set[tuple[str, str]] = set()

        async def renew_lease():
            while True:
                await asyncio.sleep(20)
                if not await repo.renew(control.lease, seconds=300):
                    raise RuntimeError("benchmark lease renewal failed")

        heartbeat = asyncio.create_task(renew_lease())
        while True:
            if index:
                fresh = WorkControl(repo, control.lease, "code-runner", {}, control.validate)
                fresh.current = await repo.get(control.current["id"])
                control = fresh
            runner = runner_kind(executor, original_runner._concurrency)
            runner.code_mode_settings = original_runner.code_mode_settings
            runner.main_contract = contract
            current_runtime = replace(
                runtime,
                work_control=control,
                fixed_tools=definitions,
                runtime_config=config,
                max_tool_calls=task.segment_tools,
                max_model_requests=60,
                canonical_conversation_id=control.lease.conversation_id,
            )
            before = await repo.get(control.current["id"])
            calls_before = len(wires)
            try:
                result = await runner.run(initial, current_runtime, backend)
            except Exception as exc:
                error = type(exc).__name__
                break
            # Use the same durable finalization as the caller/dispatcher. The
            # model's complete proposal alone has not transitioned the Work yet.
            await settle(control, delivered=False, pending_inputs=bool(await control.pending()))
            after = await repo.get(control.current["id"])
            if result.outcome is not None and result.outcome.failure is not None:
                failure_details.append(
                    {
                        "code": result.outcome.failure.code,
                        "stage": result.outcome.failure.stage,
                        "reason": result.outcome.reason.value,
                    }
                )
            segments.append(
                {
                    "index": index,
                    "state": result.work_state,
                    "durable_state": after["state"],
                    "models_before": before["model_requests"],
                    "models_after": after["model_requests"],
                    "tools_before": before["tool_calls"],
                    "tools_after": after["tool_calls"],
                    "new_physical_requests": len(wires) - calls_before,
                    "elapsed_seconds": time.perf_counter() - started,
                }
            )
            if after["state"] != "queued":
                break
            progress = {
                (b["name"], b["arguments"].get("path", "").removeprefix("/workspace/"))
                for b in business
                if b.get("ok") and b["name"] != "workspace_list"
            }
            note = json.loads(after["checkpoint_json"]).get("context_note")
            if note:
                progress.add(("context_note", json.dumps(note, sort_keys=True)))
            stagnant = stagnant + 1 if progress <= observed_progress else 0
            observed_progress.update(progress)
            if stagnant >= 10:
                stopped_for_stall = True
                break
            index += 1
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
    actual = {}
    for path in task.expected:
        try:
            actual[path] = files.read(path)["text"]
        except (OSError, WorkspaceError):
            actual[path] = None
    correct = all(
        actual[p] is not None and actual[p].strip() == expected.strip()
        for p, expected in task.expected.items()
    )
    writes = [b for b in business if b["name"] == "workspace_write" and b.get("ok")]
    write_paths = [b["arguments"]["path"].removeprefix("/workspace/") for b in writes]
    duplicate_writes = len(write_paths) - len(set(write_paths))
    mode_used = any(c["name"] == "execute_code" for t in turns for c in t["tool_calls"])
    read_paths = {
        b["arguments"].get("path", "").removeprefix("/workspace/")
        for b in business
        if b["name"] == "workspace_read" and b.get("ok")
    }
    all_inputs_read = set(task.required_reads) <= read_paths
    report_path = "batch.tsv" if task_name == "batch_ledger" else "chain.tsv"
    final_report_verified = report_path in read_paths
    direct_business = any(
        c["name"] in {"workspace_read", "workspace_write"} for t in turns for c in t["tool_calls"]
    )
    mode_followed = True if mode == "code" else not mode_used
    artifact_acceptance = (
        correct
        and duplicate_writes == 0
        and all_inputs_read
        and final_report_verified
        and mode_followed
        and error is None
    )
    row = await repo.get(control.current["id"])
    success = artifact_acceptance and row["state"] == "completed" and not stopped_for_stall
    semantic_total = sum(len(text.strip().splitlines()) for text in task.expected.values())
    semantic_correct = sum(
        expected_line == actual_line
        for path, text in task.expected.items()
        for expected_line, actual_line in zip(
            text.strip().splitlines(), (actual[path] or "").strip().splitlines(), strict=False
        )
    )
    RECORDS.append(
        {
            "loop": loop,
            "mode": mode,
            "task": task_name,
            "repeat": repeat,
            "input_sha256": task.digest(),
            "historic_source_sha256": historic_sha,
            "success": success,
            "artifact_acceptance": artifact_acceptance,
            "work_completed": row["state"] == "completed",
            "stopped_for_stall": stopped_for_stall,
            "failure_details": failure_details,
            "completion_units_correct": semantic_correct,
            "completion_units_required": semantic_total,
            "completion_fraction": semantic_correct / semantic_total,
            "correct_artifacts": correct,
            "all_inputs_read": all_inputs_read,
            "final_report_verified": final_report_verified,
            "mode_followed": mode_followed,
            "code_used": mode_used,
            "direct_business_used": direct_business,
            "error_category": error,
            "duplicate_writes": duplicate_writes,
            "duplicate_operation_ids": len(business) - len({b["operation_id"] for b in business}),
            "physical_http": len(wires),
            "business_calls": len(business),
            "business_read_calls": sum(b["name"] == "workspace_read" for b in business),
            "successful_writes": len(writes),
            "logical_models": row["model_requests"],
            "charged_business_calls": row["tool_calls"],
            "final_work_state": row["state"],
            "segments": segments,
            "first_correct_artifact_seconds": first_correct,
            "total_seconds": time.perf_counter() - started,
            "expected_artifacts": task.expected,
            "actual_artifacts": actual,
            "model_turns": turns,
            "wire": wires,
            "business_log": business,
            "request_shapes_fixed": len({w["tools_sha256"] for w in wires}) <= 1,
        }
    )
    IN_PROGRESS = None
    write_report()
    await repo.release(control.lease)
    print(
        f"BENCHMARK {task_name}/{repeat}/{loop}/{mode}: correct={correct} "
        f"success={success} http={len(wires)} business={len(business)} "
        f"seconds={time.perf_counter() - started:.2f}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorize-paid", action="store_true", required=True)
    parser.add_argument(
        "--unlimited-cost",
        action="store_true",
        help="explicit user authorization; records cost without quotas",
    )
    parser.add_argument("--max-output-tokens", type=int, choices=(8192, 32768), default=8192)
    parser.add_argument("--case", action="append", dest="selected_cases")
    parser.add_argument("--prior-report", type=Path, action="append", required=True)
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if not Path(os.environ.get("YUKI_MONTY_BINARY", "")).is_file():
        parser.error("a real Monty worker is required")
    global LEDGER, OUTPUT, STARTED, MAX_OUTPUT
    MAX_OUTPUT = args.max_output_tokens
    LEDGER = prior_ledger(args.prior_report, unlimited=args.unlimited_cost)
    OUTPUT, STARTED = args.output, datetime.now(UTC).isoformat()
    if OUTPUT.exists():
        parser.error("output exists; preserve original benchmark evidence")
    CREDENTIALS.update(read_credentials(args.credentials))
    if CREDENTIALS["model"] != "deepseek-flash":
        parser.error("review pricing before benchmarking another model")
    logging.disable(logging.CRITICAL)
    order = [
        (loop, mode, task, repeat)
        for repeat in range(args.repeats)
        for task in TASKS
        for loop in ("old", "new")
        for mode in ("direct", "code")
    ]
    random.Random(20261005).shuffle(order)
    if args.selected_cases:
        selected = set(args.selected_cases)
        available = {"/".join(map(str, case)) for case in order}
        if not selected <= available:
            parser.error("unknown benchmark case")
        order = [case for case in order if "/".join(map(str, case)) in selected]
    sys.modules["scripts.benchmark_long_tasks"] = sys.modules[__name__]
    write_report()
    try:
        with tempfile.TemporaryDirectory(prefix="yuki-long-tasks-") as temporary:
            path = Path(temporary) / "test_long_tasks.py"
            path.write_text(
                "import pytest\nfrom scripts.benchmark_long_tasks import compare_case\n"
                "@pytest.mark.parametrize('loop,mode,task,repeat', " + repr(order) + ")\n"
                "async def test_long_tasks(database,tmp_path,loop,mode,task,repeat):\n"
                "    await compare_case(database,tmp_path,loop,mode,task,repeat)\n"
            )
            result = pytest.main(
                [
                    "-c",
                    str(ROOT / "pyproject.toml"),
                    "-q",
                    "-s",
                    "--tb=short",
                    "-p",
                    "no:warnings",
                    "-p",
                    "tests.conftest",
                    "--basetemp",
                    str(Path(temporary) / "fixtures"),
                    str(path),
                ]
            )
        write_report()
        return int(result)
    finally:
        CREDENTIALS.clear()


if __name__ == "__main__":
    raise SystemExit(main())
