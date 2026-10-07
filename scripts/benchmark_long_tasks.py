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
from tests.support.parent_receipts import observation_bodies

from scripts.benchmark_pi_codemode import BASELINE, historical_runner
from scripts.export_pi_codemode_inventory import export_inventory
from scripts.verify_deepseek_codemode import read_credentials

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 8192
SEGMENT_TOOLS: int | None = None
REASONING_EFFORT = "low"
DEFAULT_CODE_POLICY = False
RECORDS: list[dict[str, Any]] = []
CREDENTIALS: dict[str, str] = {}
OUTPUT: Path | None = None
STARTED = ""
IN_PROGRESS: dict[str, Any] | None = None
LEDGER: BudgetLedger | None = None
TASKS = ("batch_ledger", "dependency_chain", "resumed_work")


def orchestration_guidance(mode: str, *, default_policy: bool = False) -> str:
    """Default-policy acceptance supplies the real contract, never a canned program."""
    from qq_ai_bot.prompting.contracts import CORE_CONTRACT

    prefix = CORE_CONTRACT + "\n\n" if default_policy else ""
    if mode == "direct":
        return prefix + (
            ("Experimental direct-call control: " if default_policy else "")
            + "Use direct workspace_read/workspace_write/workspace_list calls only; never "
            "execute_code. You may batch independent direct calls in one response."
        )
    if mode != "code":
        raise ValueError("unknown orchestration mode")
    return (
        prefix
        + (
            "Follow the shared default orchestration strategy for this accepted Work. "
            "Choose your own approach and program; direct calls remain available "
            "for its exceptions. "
            if default_policy
            else "Code Mode is enabled: execute_code is available for loops and aggregation. "
            "Choose your own approach and program; direct workspace calls are also allowed. "
        )
        + "Use await yuki_workspace_read/write with the same schemas and receipt data.text."
    )


def observe_call(call: Any) -> dict[str, Any]:
    """Instrumentation must not intercept malformed arguments before the Host."""
    entry: dict[str, Any] = {"id": call.id, "name": call.function.name}
    try:
        entry["arguments"] = json.loads(call.function.arguments)
    except (TypeError, ValueError):
        entry.update(
            arguments=None, arguments_raw=call.function.arguments, arguments_valid_json=False
        )
    return entry


async def resumed_control(control: Any) -> Any:
    """Rebind the same trusted caller while retaining the original Work/lease."""
    from qq_ai_bot.runtime.work_control import WorkControl

    if control.context_access is None:
        raise ValueError("benchmark_read_identity_missing")
    fresh = WorkControl(
        control.repository,
        control.lease,
        control.source_key,
        dict(control.source),
        control.validate,
    )
    fresh.bind_context_access(control.context_access)
    fresh.current = await control.repository.get(control.current["id"])
    return fresh


async def pending_code_boundaries(control: Any) -> list[dict[str, Any]]:
    """Observe original live VM checkpoints, never execute or invent progress."""
    from sqlalchemy import select

    from qq_ai_bot.runtime.work_schema_v1 import effects

    if control.session is None or control.current is None:
        return []
    async with control.repository.database.sessions() as reader:
        keys = await reader.scalars(
            select(effects.c.effect_key).where(
                effects.c.work_id == control.current["id"],
                effects.c.kind == "code_composition",
                effects.c.state.in_(("prepared", "unknown")),
            )
        )
        candidates = list(keys)
    boundaries = []
    for key in candidates:
        boundary = await control.session.journal.unsettled_composition(control.current["id"], key)
        if boundary is not None and boundary["snapshot_ref"] is not None:
            boundaries.append(boundary)
    return boundaries


def advancing_code_boundaries(
    boundaries: list[dict[str, Any]], previous: dict[str, int]
) -> set[tuple[str, str]]:
    """Only continuation of the same original operation counts as VM progress."""
    progress = set()
    for boundary in boundaries:
        key, revision = boundary["operation_id"], boundary["snapshot_revision"]
        before = previous.get(key)
        if before is not None and revision > before:
            progress.add(("code_boundary", f"{key}:{revision}"))
        previous[key] = max(revision, before or 0)
    return progress


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

    def admit(
        self, byte_count: int, output_limit: int, *, maximum_output: int | None = None
    ) -> tuple[int, float]:
        bound = MAX_OUTPUT if maximum_output is None else maximum_output
        if not byte_count > 0 or not 1 <= output_limit <= bound:
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
            "without a header. Read batch.tsv back with workspace_read and verify its text before "
            "completing. File size and write receipts alone do not satisfy this readback."
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
        "in visit order, then TOTAL<TAB>sum. No header. Read chain.tsv back with workspace_read "
        "and verify its text before completing. File size and write receipts alone do not "
        "satisfy this readback. "
        + (
            "After reading each chosen node, also create audit/00.txt, audit/01.txt, etc. "
            "immediately before reading the next node or writing chain.tsv, in visit order, "
            "each containing that node value and a newline. Each audit file "
            "must be written exactly once. The host segments work after five business calls; "
            "continue the original Work when it resumes."
            if audit
            else ""
        )
    )
    return Task(
        name, files, expected, instruction, 5 if audit else 80, depth + 1, ("start.txt", *paths)
    )


def audit_order_verified(task: Task, business: list[dict[str, Any]]) -> bool:
    """Audit each node before following it; correct final files alone cannot prove order."""
    if task.name != "resumed_work":
        return True
    reads, writes = {}, {}
    for index, call in enumerate(business):
        if not call.get("ok"):
            continue
        path = call["arguments"].get("path", "").removeprefix("/workspace/")
        if call["name"] == "workspace_read":
            reads.setdefault(path, index)
        elif call["name"] == "workspace_write":
            writes.setdefault(path, []).append(index)
    nodes = task.required_reads[1:]
    for index, path in enumerate(nodes):
        audit = writes.get(f"audit/{index:02d}.txt", [])
        following = (
            reads.get(nodes[index + 1])
            if index + 1 < len(nodes)
            else next(iter(writes.get("chain.tsv", [])), None)
        )
        if (
            path not in reads
            or len(audit) != 1
            or following is None
            or not reads[path] < audit[0] < following
        ):
            return False
    return True


def write_report() -> None:
    if OUTPUT is None or LEDGER is None:
        return
    report = {
        "started_at": STARTED,
        "updated_at": datetime.now(UTC).isoformat(),
        "model": CREDENTIALS.get("model"),
        "baseline_commit": BASELINE,
        "baseline_scope": (
            "same current runtime, frozen declarations and synthetic inputs; experimental "
            "direct-call control compared with shared default orchestration guidance"
            if DEFAULT_CODE_POLICY
            else "historic direct iteration unchanged; historic code iteration has "
            "two explicit yield/resume compatibility hooks; common current invocation/code "
            "helpers; not an unmodified deployed old application"
        ),
        "protocol": "chat_completions",
        "fixed_declarations": next(
            iter(
                {
                    w["tools_count"]
                    for r in RECORDS
                    for w in r.get("wire", [])
                    if w.get("purpose", "main") == "main"
                }
            ),
            None,
        ),
        "max_output_tokens": MAX_OUTPUT,
        "segment_tools_override": SEGMENT_TOOLS,
        "reasoning_effort": REASONING_EFFORT,
        "default_code_policy": DEFAULT_CODE_POLICY,
        "model_tool_exposure": "tiered" if DEFAULT_CODE_POLICY else "full historical control",
        "context_measurement": "UTF-8 serialized messages and tool receipt characters per "
        "physical request; measured separately from the fixed tool declarations and provider "
        "reported prompt tokens; no raw reasoning retained",
        "cost_estimate_basis": "Common Peak Flash tariff verified at "
        "https://api-docs.deepseek.com/quick_start/pricing/ on 2026-10-06; "
        "not a provider invoice or the actual time-of-day tariff. Token counts and elapsed "
        "time are observed separately.",
        "completion_definition": "independent oracle correct, all required inputs read, report "
        "verified, per-node audits committed before following the next node or writing the "
        "final chain, no repeated committed writes, and original Work durably completed",
        "acceptance_definition": "success additionally requires the assigned tool mode; "
        "default-policy code trials must actually choose execute_code. goal_completed "
        "records task completion separately from orchestration acceptance",
        "stop_policy": "runtime pause/failure, or 10 consecutive activations with no new "
        "successful read/write path, semantic context note or same original code "
        "snapshot advancing across activations; no task time ceiling",
        "business_scope": "temporary FileWorkspace, real SQLite/InvocationService/Monty, "
        "no production, gateway or real messaging",
        "recovery_scope": "fresh Runner and WorkControl activations in same process; "
        "not an OS process crash or provider disconnect",
        "read_identity": "canonical synthetic person bound before accept and on every activation",
        "receipt_errors_scope": "visible tool history per HTTP request; repeated history is "
        "not a new failed operation",
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime_source_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in (
                "src/qq_ai_bot/codemode/engine_monty.py",
                "src/qq_ai_bot/runtime/work_control.py",
                "src/qq_ai_bot/runtime/work_session.py",
                "src/qq_ai_bot/services/turn_execution.py",
                "src/qq_ai_bot/codemode/contract.py",
                "scripts/benchmark_pi_codemode.py",
                "tests/integration/test_codemode_runner.py",
            )
        },
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
    from qq_ai_bot.runtime.work_context_note import visible_context_note
    from qq_ai_bot.runtime.work_supervisor import settle
    from qq_ai_bot.workspace.files import FileWorkspace
    from qq_ai_bot.workspace.store import WorkspaceError

    global IN_PROGRESS
    assert LEDGER is not None
    task = make_task(task_name, repeat)
    if SEGMENT_TOOLS is not None:
        task = replace(
            task,
            segment_tools=SEGMENT_TOOLS,
            instruction=task.instruction.replace(
                "five business calls", f"{SEGMENT_TOOLS} business calls"
            ),
        )
    workspace_path = (tmp_path / "synthetic-workspace").resolve()
    workspace_path.mkdir()
    files = FileWorkspace(workspace_path)
    for path, text in task.files.items():
        files.write(path, text.encode())
    # Build today's frozen declarations in an isolated synthetic assembly.
    # The P00 inventory is historical evidence, not the current tool contract.
    inventory = await export_inventory()
    definitions = tuple(ChatTool(**row) for row in inventory["frozen_definitions"])
    public_definitions = definitions
    if DEFAULT_CODE_POLICY:
        from qq_ai_bot.codemode.tool_visibility import model_definitions

        public_definitions = model_definitions(definitions)
    revision = inventory["manifest_revision"]
    chat, _, control, runtime, repo = await runner_env(database, tmp_path, iter(()))
    original_runner = chat.runtime.runner
    runner_kind = type(original_runner)
    historic_sha = None
    if loop == "old":
        runner_kind, historic_sha = historical_runner(code_mode=mode == "code")
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
        purpose = "main" if payload.get("tools") else "work_compaction"
        # The existing compaction window has its own output contract. Reserve
        # all its usage without mistaking the primary CLI limit for that limit.
        bound = MAX_OUTPUT if purpose == "main" else config.context.compaction_output_tokens
        identity, reserve = LEDGER.admit(
            len(request.content), payload.get("max_tokens", 0), maximum_output=bound
        )
        request.extensions["benchmark_ledger_id"] = identity
        tools = json.dumps(payload.get("tools", []), sort_keys=True).encode()
        # Only synthetic tool receipts are retained, never raw reasoning or headers.
        errors = []
        error_call_ids = []
        observed = []
        receipt_characters = 0
        for message in payload.get("messages", []):
            receipts = []
            if message.get("role") == "tool":
                receipts.append((message.get("tool_call_id"), message.get("content", "")))
            elif message.get("role") == "user":
                for material in observation_bodies(message.get("content", "")):
                    if material.get("kind") == "work_unobserved_tool_round":
                        receipts.extend(
                            (item["call_id"], item["result"]) for item in material["calls"]
                        )
            for call_id, content in receipts:
                # Count portable rebase evidence as well as protocol tool rows;
                # result characters follow the original parent ID on either wire.
                receipt_characters += len(content)
                observed.append(
                    {"call_id": call_id, "sha256": hashlib.sha256(content.encode()).hexdigest()}
                )
                try:
                    receipt = json.loads(content)
                    if isinstance(receipt, dict) and (
                        receipt.get("error") or not receipt.get("ok", True)
                    ):
                        errors.append(receipt)
                        error_call_ids.append(call_id)
                except (ValueError, TypeError):
                    pass
        wires.append(
            {
                "ledger_id": identity,
                "request_bytes": len(request.content),
                "message_bytes": len(
                    json.dumps(payload.get("messages", []), ensure_ascii=False).encode()
                ),
                "tool_receipt_characters": receipt_characters,
                "bytes": len(request.content),
                "output_limit": payload.get("max_tokens", 0),
                "output_limit_bound": bound,
                "reserved_usd": reserve,
                "tools_sha256": hashlib.sha256(tools).hexdigest(),
                "tools_count": len(payload.get("tools", [])),
                "purpose": purpose,
                "receipt_errors": errors,
                "receipt_error_call_ids": error_call_ids,
                "observed_receipts": observed,
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
                    "tool_calls": [observe_call(c) for c in result.tool_calls],
                    "final_text": result.content if not result.tool_calls else None,
                }
            )
            return result

    base = CREDENTIALS["base_url (openai)"].rstrip("/") + "/"
    mode_instruction = orchestration_guidance(mode, default_policy=DEFAULT_CODE_POLICY)
    instruction = (
        "This is an isolated long-task benchmark. Work already accepted. Only temporary "
        "workspace_read/workspace_write/workspace_list are authorized; never send messages, "
        "use memory, terminal, network or other business tools. "
        "Task_control lifecycle is available. "
        "Working tool history retires at business segment boundaries; the valid context_note "
        "and last unseen receipts survive, not earlier unpersisted working state. For segmented "
        "tasks, save cumulative findings, necessary intermediate values, completed steps and "
        "next steps via task_control(action='update', context_note=...) before spending the "
        "last business call; merge a previous note and fresh receipts when resuming. Note calls "
        "do not consume business allowance. Use version=1 and facts/unresolved/next_steps, "
        "each containing text and valid refs as declared by task_control. "
        "Receipts contain data.text on reads and data.path/version on writes. CSV fields "
        "contain no quotes or commas; node files are key=value text. In Code Mode use "
        "string methods and Python builtins, or Monty built-in modules such as math. "
        "Import asyncio explicitly before asyncio.gather. Tool receipts are dictionaries: "
        "use r['ok'] and r['data'], never r.ok. Host Python packages are unavailable. "
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
            reasoning_effort=ReasoningEffort(REASONING_EFFORT),
            # Long reasoning can trigger the real Work compactor. Its tool-free
            # JSON request needs the same supported capability as production.
            capabilities={
                ModelCapability.TOOLS,
                ModelCapability.REASONING,
                ModelCapability.STRUCTURED_OUTPUT,
            },
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
        code_revisions: dict[str, int] = {}

        async def renew_lease():
            while True:
                await asyncio.sleep(20)
                if not await repo.renew(control.lease, seconds=300):
                    raise RuntimeError("benchmark lease renewal failed")

        heartbeat = asyncio.create_task(renew_lease())
        while True:
            if index:
                control = await resumed_control(control)
            runner = runner_kind(executor, original_runner._concurrency)
            runner.code_mode_settings = original_runner.code_mode_settings
            runner.main_contract = contract
            current_runtime = replace(
                runtime,
                work_control=control,
                fixed_tools=public_definitions,
                runtime_config=config,
                max_tool_calls=task.segment_tools,
                max_model_requests=60,
                canonical_conversation_id=control.lease.conversation_id,
            )
            before = await repo.get(control.current["id"])
            saved_note = json.loads(before["checkpoint_json"]).get("context_note")
            visible_note = await visible_context_note(control)
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
            boundaries = await pending_code_boundaries(control)
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
                    "context_note_saved_at_start": bool(saved_note),
                    "context_note_visible_at_start": visible_note is not None,
                    "pending_code_boundaries": boundaries,
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
                # Administrative note revision/call IDs are not semantic progress.
                progress.add(("context_note", json.dumps(note.get("payload"), sort_keys=True)))
            progress.update(advancing_code_boundaries(boundaries, code_revisions))
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
    mode_followed = (not DEFAULT_CODE_POLICY or mode_used) if mode == "code" else not mode_used
    audit_order = audit_order_verified(task, business)
    artifact_acceptance = (
        correct
        and duplicate_writes == 0
        and all_inputs_read
        and final_report_verified
        and audit_order
        and error is None
    )
    row = await repo.get(control.current["id"])
    goal_completed = artifact_acceptance and row["state"] == "completed" and not stopped_for_stall
    success = goal_completed and mode_followed
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
            "work_id": row["id"],
            "repeat": repeat,
            "input_sha256": task.digest(),
            "instruction_sha256": hashlib.sha256(task.instruction.encode()).hexdigest(),
            "segment_tools": task.segment_tools,
            "historic_source_sha256": historic_sha,
            "historic_code_adapter": bool(getattr(runner_kind, "benchmark_code_adapter", False)),
            "historic_adapter_sha256": getattr(runner_kind, "benchmark_adapter_sha256", None),
            "success": success,
            "goal_completed": goal_completed,
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
            "audit_order_verified": audit_order,
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
            "request_shapes_fixed": len(
                {w["tools_sha256"] for w in wires if w["purpose"] == "main"}
            )
            <= 1,
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
    parser.add_argument("--segment-tools", type=int, choices=(5, 32), default=None)
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="low")
    parser.add_argument(
        "--default-code-policy",
        action="store_true",
        help="compare current direct control with shared guidance; require real Code Mode choice",
    )
    parser.add_argument("--case", action="append", dest="selected_cases")
    parser.add_argument("--prior-report", type=Path, action="append", required=True)
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if not Path(os.environ.get("YUKI_MONTY_BINARY", "")).is_file():
        parser.error("a real Monty worker is required")
    global LEDGER, OUTPUT, STARTED, MAX_OUTPUT, SEGMENT_TOOLS, REASONING_EFFORT, DEFAULT_CODE_POLICY
    MAX_OUTPUT = args.max_output_tokens
    SEGMENT_TOOLS = args.segment_tools
    REASONING_EFFORT = args.reasoning_effort
    DEFAULT_CODE_POLICY = args.default_code_policy
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
        for loop in (("new",) if DEFAULT_CODE_POLICY else ("old", "new"))
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
        # A passed harness collection is not model-task acceptance. Default
        # policy verification must fail the CLI when its independent oracle fails.
        rejected = DEFAULT_CODE_POLICY and (
            not RECORDS or any(not row["success"] for row in RECORDS)
        )
        return int(result) or int(rejected)
    finally:
        CREDENTIALS.clear()


if __name__ == "__main__":
    raise SystemExit(main())
