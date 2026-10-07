"""Four isolated assemblies; fixed historic loop is loaded only for this experiment.

Run with PYTHONPATH=. YUKI_MONTY_BINARY=... uv run --frozen python
scripts/benchmark_pi_codemode.py --output <evidence.json>. No real Provider/send.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

BASELINE = "b4fdef7d0e2f7ee68fd345605d0a8fadc50b5459"
ROOT = Path(__file__).resolve().parents[1]
RECORDS: list[dict[str, Any]] = []


def historical_runner(*, code_mode: bool = False) -> tuple[type, str]:
    """Historic iteration, with an explicit test-only Code yield/resume adapter."""
    from qq_ai_bot.services.agent_runner import AgentRunner

    result = subprocess.run(
        ["git", "show", f"{BASELINE}:src/qq_ai_bot/services/agent_runner.py"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    source = result.stdout
    adapted = source.decode()
    # APP-04 removed the legacy Provider shim. This experiment supplies an
    # explicit TaskModelExecutor; reject the unsupported constructor branch
    # locally instead of resurrecting a production compatibility symbol.
    # Shared Code execution records child facts itself; a parent summary is not
    # another business effect and need not carry an outer-call evidence entry.
    replacements = {
        (
            "from qq_ai_bot.model_runtime.executor import "
            "ModelCompleter, ModelExecutor, require_model_executor"
        ): "from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor",
        """            self._models = require_model_executor(
                None,
                provider=cast(ModelCompleter, model_executor),
            )""": (
            "            raise TypeError('historic benchmark requires explicit ModelExecutor')"
        ),
        "from qq_ai_bot.services.work_reporting import (": (
            "from _yuki_benchmark_historic_reporting import ("
        ),
        """                    runtime.work_control.observe_result(
                        call.function.name,
                        result,
                        _was_executed,
                        side_effecting=self._is_side_effecting(tools, call, runtime),
                        arguments=call.function.arguments,
                    )""": """                    if call.id in coordinated.evidence:
                        runtime.work_control.observe_evidence(
                            coordinated.evidence[call.id]
                        )""",
    }
    for old, new in replacements.items():
        if adapted.count(old) != 1:
            raise ValueError("historic_kernel_adapter_source_changed")
        adapted = adapted.replace(old, new, 1)
    # The historical loop already used ordered continuation_items; the two
    # removed legacy planes were only cleared with empty tuples. Preserve that
    # exact empty meaning rather than creating parallel continuation ownership.
    for retired_plane in ("continuation_messages", "function_outputs"):
        lines = adapted.splitlines(keepends=True)
        clearing = [line for line in lines if line.strip() == f"{retired_plane}=(),"]
        if len(clearing) != 3:
            raise ValueError("historic_continuation_adapter_source_changed")
        adapted = "".join(line for line in lines if line not in clearing)
    # Preserve the original reporting policy beside the original loop. These
    # functions live only in this isolated experiment module, never src aliases.
    reporting_source = subprocess.run(
        ["git", "show", f"{BASELINE}:src/qq_ai_bot/services/work_reporting.py"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    reporting = ModuleType("_yuki_benchmark_historic_reporting")
    exec(
        compile(reporting_source, f"git:{BASELINE}:work_reporting.py", "exec"),
        reporting.__dict__,
    )
    sys.modules[reporting.__name__] = reporting
    if code_mode:
        # The old version predates Code Mode. Attaching the helper alone left
        # snapshots orphaned: its iteration never resumed them or handled yield.
        # Only these two test-only hooks change the baseline iteration.
        restore_anchor = (
            "            repeated_batch_count = int("
            'runtime.work_control.session.progress.get("repeats", 0))'
        )
        restore_bridge = """\
            pending_code = runtime.work_control.session.pending_compositions
            if pending_code and await self._resume_compositions(
                pending_code, transcript, tools, runtime, fixed_definitions
            ):
                return AgentRunResult(
                    text="", tool_calls_used=0, model_requests=0,
                    web_was_used=False, suppress_delivery=True, work_state="queued"
                )
"""
        batch_anchor = "            batch, executed = coordinated.calls, coordinated.executed_count"
        batch_bridge = """\
            if any(
                result == _CODE_COMPOSITION_YIELDED for _, result, _ in coordinated.calls
            ):
                runtime.work_control.yield_segment = True
                runtime.work_control.ending = "queued"
                return AgentRunResult(
                    text="", tool_calls_used=calls_used, model_requests=request_index + 1,
                    web_was_used=web_was_used, suppress_delivery=True, work_state="queued"
                )
"""
        for anchor, bridge in ((restore_anchor, restore_bridge), (batch_anchor, batch_bridge)):
            anchor = "\n" + anchor + "\n"
            if adapted.count(anchor) != 1:
                raise ValueError("historic_code_adapter_source_changed")
            adapted = adapted.replace(anchor, "\n" + bridge + anchor.lstrip("\n"), 1)
    module = ModuleType("_yuki_benchmark_historic_runner")
    from qq_ai_bot.services.agent_runner import CODE_COMPOSITION_YIELDED

    module._CODE_COMPOSITION_YIELDED = CODE_COMPOSITION_YIELDED
    sys.modules[module.__name__] = module
    exec(compile(adapted, f"git:{BASELINE}:agent_runner.py", "exec"), module.__dict__)
    historic = module.AgentRunner
    historic.benchmark_adapter_sha256 = hashlib.sha256(adapted.encode()).hexdigest()
    historic.benchmark_reporting_sha256 = hashlib.sha256(reporting_source).hexdigest()
    historic.benchmark_code_adapter = code_mode
    # Common kernels, with the advertised hooks rather than the current Pi loop.
    for name in (
        "_execute_tool_batch",
        "_execute_tool_batch_impl",
        "_resume_compositions",
        # The current resume kernel budgets selected media before pairing.
        "_budget_tool_media",
        "_code_host",
        "_execute_code_batch",
        "_run_code_call",
        "_execute_control_call",
    ):
        setattr(historic, name, getattr(AgentRunner, name))
    return historic, hashlib.sha256(source).hexdigest()


async def compare_case(database: Any, tmp_path: Path, loop: str, mode: str, scenario: str) -> None:
    from tests.integration.test_codemode_provider_wire import answer
    from tests.integration.test_codemode_runner import ACCEPT, Backend, call, runner_env

    from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ModelResponseStatus
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
    from qq_ai_bot.runtime.work_journal import WorkJournal, decode_transcript

    if mode == "direct":
        steps = [
            ChatResponse(
                "",
                0,
                tool_calls=(
                    call("lookup", {"q": 1}, "read1").tool_calls[0],
                    call("lookup", {"q": 2}, "read2").tool_calls[0],
                ),
            ),
            call("workspace_write", {"value": 3}, "write"),
        ]
    else:
        steps = [
            call(
                "execute_code",
                {
                    "code": "a = await yuki_lookup({'q': 1})\n"
                    "b = await yuki_lookup({'q': 2})\n"
                    "w = await yuki_workspace_write({'value': a['data']['q'] + b['data']['q']})\n"
                    "a['data']['q'] + b['data']['q']"
                },
                "outer",
            )
        ]
    final = "3"
    if scenario == "unauthorized":
        steps = [
            call(
                "not_declared" if mode == "direct" else "execute_code",
                {} if mode == "direct" else {"code": "await yuki_not_declared({})"},
                "bad",
            )
        ]
        # The state-change Work cannot be completed after a refused mutation.
        # Both groups explicitly fail the original Work instead of claiming success.
        steps.append(call("task_control", {"action": "fail", "reason": "denied"}, "fail"))
        final = "denied"
    elif scenario == "incomplete":
        bad = call("workspace_write" if mode == "direct" else "execute_code", {}, "truncated")
        steps.insert(0, replace(bad, status=ModelResponseStatus.INCOMPLETE))
    responses = iter([call("task_control", ACCEPT, "accept"), *steps, ChatResponse(final, 0)])
    chat, _, control, runtime, repo = await runner_env(database, tmp_path, iter(()))
    runner = chat.runtime.runner
    historic_sha = None
    if loop == "old":
        kind, historic_sha = historical_runner(code_mode=mode == "code")
        old = kind(runner._models, runner._concurrency)
        old.code_mode_settings, old.main_contract = runner.code_mode_settings, runner.main_contract
        runner = old
    payloads: list[bytes] = []
    start = time.perf_counter()

    def transport(request: httpx.Request) -> httpx.Response:
        payloads.append(request.content)
        scripted = next(responses)
        body = answer(scripted, "chat_completions", len(payloads))
        if scripted.status == ModelResponseStatus.INCOMPLETE:
            body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=body)

    class Downstream(Backend):
        def __init__(self):
            super().__init__()
            self.first_useful: float | None = None
            self.writes: list[int] = []

        async def execute_call(self, invocation):
            if invocation.call.function.name == "workspace_write":
                self.writes.append(json.loads(invocation.call.function.arguments)["value"])
                self.first_useful = time.perf_counter() - start
            return await super().execute_call(invocation)

    async with httpx.AsyncClient(
        base_url="https://benchmark.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = OpenAICompatibleProvider(
            base_url="https://benchmark.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
            provider_name="deepseek",
        )
        profile = ModelProfile(
            id="benchmark",
            provider="deepseek",
            protocol=ModelProtocol.CHAT_COMPLETIONS,
            base_url="https://benchmark.invalid/",
            api_key_env="UNUSED",
            model="synthetic",
            timeout_seconds=2,
            max_retries=0,
            default_max_output_tokens=4096,
            default_temperature=0.5,
            capabilities={ModelCapability.TOOLS, ModelCapability.REASONING},
        )
        runner._models = TaskModelExecutor(
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
        result = await runner.run(
            (ChatMessage("user", "fixed isolated task"),),
            replace(
                runtime,
                max_tool_calls=8,
                max_model_requests=8,
                canonical_conversation_id=control.lease.conversation_id,
            ),
            backend,
        )
        elapsed = time.perf_counter() - start
        assert result.text == final
        if scenario == "unauthorized":
            assert control.ending == "failed"
        assert backend.writes == ([] if scenario == "unauthorized" else [3])
        assert len(backend.log) == (0 if scenario == "unauthorized" else 3)
        assert len({identity for _, identity in backend.log}) == len(backend.log)
        row = await repo.get(control.current["id"])
        assert row["tool_calls"] == len(backend.log)
        # Verify the existing reader can recover the exact paired checkpoint and
        # counters. This is checkpoint readability, not a process-crash benchmark.
        await control.session.save("paired")
        loaded = await WorkJournal(repo).load(
            control.lease, control.current["id"], control.session.contract, source_control=control
        )
        assert loaded.record is not None
        restored = decode_transcript(json.loads(loaded.record["payload_json"])["transcript"])
        assert restored.request() == control.session.transcript.request()
        after = await repo.get(control.current["id"])
        assert (after["tool_calls"], after["model_requests"]) == (
            row["tool_calls"],
            row["model_requests"],
        )
        RECORDS.append(
            {
                "loop": loop,
                "mode": mode,
                "task": scenario,
                "success": scenario != "unauthorized",
                "expected_refusal": scenario == "unauthorized",
                "correctness": True,
                "logical_model_rounds": result.model_requests,
                "physical_http": len(payloads),
                "business_subcalls": len(backend.log),
                "tool_budget_used": row["tool_calls"],
                "request_budget_used": row["model_requests"],
                "paired_checkpoint_readable": True,
                "unauthorized_effects": 0,
                "duplicate_effects": 0,
                "first_useful_artifact_seconds": backend.first_useful,
                "total_seconds": elapsed,
                "tokens": None,
                "cache_tokens": None,
                "unknown_usage_calls": len(payloads),
                "billable_cost": None,
                "transport_cost": 0,
                "historic_source_sha256": historic_sha,
                "historic_code_adapter": bool(
                    getattr(type(runner), "benchmark_code_adapter", False)
                ),
                "historic_adapter_sha256": getattr(type(runner), "benchmark_adapter_sha256", None),
                "historic_reporting_sha256": getattr(
                    type(runner), "benchmark_reporting_sha256", None
                ),
            }
        )
    await repo.release(control.lease)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not Path(os.environ.get("YUKI_MONTY_BINARY", "")).is_file():
        parser.error("a real Monty binary is required")
    # Pytest owns fresh SQLite fixtures for every group/task. Only this temporary
    # collection is generated; no old production loop becomes a CI dependency.
    sys.modules["scripts.benchmark_pi_codemode"] = sys.modules[__name__]
    with tempfile.TemporaryDirectory(prefix="yuki-pi-comparison-") as temporary:
        path = Path(temporary) / "test_comparison.py"
        path.write_text("""import pytest
from scripts.benchmark_pi_codemode import compare_case
@pytest.mark.parametrize("loop", ["old", "new"])
@pytest.mark.parametrize("mode", ["direct", "code"])
@pytest.mark.parametrize("scenario", ["fanout", "incomplete", "unauthorized"])
async def test_comparison(database, tmp_path, loop, mode, scenario):
    await compare_case(database, tmp_path, loop, mode, scenario)
""")
        code = pytest.main(
            [
                "-c",
                str(ROOT / "pyproject.toml"),
                "-q",
                "-p",
                "no:warnings",
                "-p",
                "tests.conftest",
                str(path),
            ]
        )
    if len(RECORDS) != 12:
        code = 1
    evidence = {
        "baseline_commit": BASELINE,
        "scope": "synthetic task planning, MockTransport, real SQLite/Monty",
        "limits": {"model_requests": 8, "business_calls": 8},
        "exit_code": int(code),
        "records": RECORDS,
        "limitations": [
            "Synthetic planning does not measure real model correctness, tokens, cache or billing.",
            "Historic main iteration is unchanged; invocation and Code Mode kernel "
            "are shared test assembly.",
            "Recovery metric verifies paired reader/counter preservation, not restart latency.",
            "No real sends, credentials, production data or network calls.",
        ],
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
