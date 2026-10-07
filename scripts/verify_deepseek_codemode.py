"""Opt-in paid Provider acceptance with real adapters/Runner and fake business effects.

Credentials are read only in memory. No gateway, production database or real
workspace is connected. This script is never collected by the normal test suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORDS: list[dict[str, Any]] = []
CREDENTIALS: dict[str, str] = {}
PHYSICAL_LIMIT = 24
MAX_OUTPUT = 4096
RESERVED_USD = 0.0
PHYSICAL_CALLS = 0


def read_credentials(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = [part.strip().strip("`") for part in line.split("|")]
        if len(fields) >= 4:
            values[fields[1].lower()] = fields[2]
    required = {"api_key", "model", "base_url (openai)", "base_url (anthropic)"}
    if not required <= values.keys():
        raise ValueError("credential file lacks the required table fields")
    # Endpoints are fixed by the user's credential document, never inferred from content.
    if any(
        urlsplit(values[key]).scheme != "https"
        or urlsplit(values[key]).hostname != "api.deepseek.com"
        or urlsplit(values[key]).username is not None
        for key in required
        if "base_url" in key
    ):
        raise ValueError("unexpected Provider endpoint")
    return values


def resume_budget(previous: dict[str, Any]) -> tuple[int, float]:
    if previous.get("resume_report") is not None:
        raise ValueError("nested resume requires a separately reviewed acceptance run")
    wires = [wire for row in previous["records"] for wire in row["wire"]]
    if len(wires) != previous["actual_physical_calls"]:
        raise ValueError("resume request counter does not match observed wire records")
    reserved = 0.0
    for wire in wires:
        size, output = wire["bytes"], wire["output_limit"]
        if not 0 < size <= 100000 or not 1 <= output <= MAX_OUTPUT:
            raise ValueError("resume wire budget is outside the bounded harness")
        reserved += (size * 0.30 + output * 1.20) / 1_000_000
    if abs(reserved - previous["conservative_reserved_usd"]) > 1e-9 or reserved > 1:
        raise ValueError("resume cost reservation does not match observed wire records")
    return len(wires), reserved


async def provider_case(database: Any, tmp_path: Path, protocol: str, case: str) -> None:
    global PHYSICAL_CALLS, RESERVED_USD
    from tests.integration.test_codemode_runner import runner_env
    from tests.support.agent_backend import StubAgentBackend

    from qq_ai_bot.codemode.api_projection import project
    from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ReasoningEffort
    from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
    from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
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

    inventory = json.loads(
        (ROOT / "docs/architecture/pi-codemode-capability-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    definitions = tuple(ChatTool(**row) for row in inventory["frozen_definitions"])
    revision = inventory["manifest_revision"]
    chat, _, control, runtime, repo = await runner_env(database, tmp_path, iter(()))
    runner = chat.runtime.runner
    runner.main_contract = SimpleNamespace(
        revision=revision, script_api=project(definitions, revision)
    )
    accepted = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": "isolated Provider test",
                "output_kind": "state_change",
                "reporting": "quiet",
                "deliver_artifacts": False,
            },
            "host-accept",
        )
    )
    if not accepted.get("ok"):
        raise RuntimeError("isolated work could not be accepted")
    wire: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    payloads: list[bytes] = []
    pending_reasoning: list[tuple[str, object]] = []
    replay_checks: list[dict[str, Any]] = []
    started = time.perf_counter()

    async def request_hook(request: httpx.Request) -> None:
        global PHYSICAL_CALLS, RESERVED_USD
        body = request.content
        payload = json.loads(body)
        output_limit = payload.get("max_tokens", payload.get("max_output_tokens", MAX_OUTPUT))
        if not isinstance(output_limit, int) or not 1 <= output_limit <= MAX_OUTPUT:
            raise RuntimeError("request output budget exceeded")
        # Byte count conservatively bounds input tokenization for this synthetic text.
        reserve = (len(body) * 0.30 + output_limit * 1.20) / 1_000_000
        if len(body) > 100000 or PHYSICAL_CALLS >= PHYSICAL_LIMIT or RESERVED_USD + reserve > 1.0:
            raise RuntimeError("paid test request/cost ceiling reached")

        def objects(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from objects(child)
            elif isinstance(value, list):
                for child in value:
                    yield from objects(child)

        visible = list(objects(payload))
        for key, material in pending_reasoning:
            replayed = any(item.get(key) == material for item in visible)
            replay_checks.append(
                {
                    "field": key,
                    "sha256": hashlib.sha256(
                        json.dumps(material, sort_keys=True).encode()
                    ).hexdigest(),
                    "replayed_exactly": replayed,
                }
            )
            if not replayed:
                raise RuntimeError("reasoning continuation changed before paid dispatch")
        pending_reasoning.clear()
        PHYSICAL_CALLS += 1
        RESERVED_USD += reserve
        payloads.append(body)
        wire.append(
            {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "output_limit": output_limit,
                "elapsed_seconds": time.perf_counter() - started,
            }
        )

    async def response_hook(response: httpx.Response) -> None:
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            body = {}
        usage = body.get("usage")
        wire[-1].update(
            {
                "http_status": response.status_code,
                "usage": usage if isinstance(usage, dict) else None,
                "response_status": body.get("status"),
                "stop_reason": body.get("stop_reason"),
                "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
            }
        )
        if protocol == "chat_completions":
            message = (body.get("choices") or [{}])[0].get("message", {})
            if message.get("tool_calls"):
                for key in ("reasoning_content", "reasoning_details"):
                    if message.get(key):
                        pending_reasoning.append((key, message[key]))
        elif protocol == "responses":
            items = body.get("output", [])
            if any(item.get("type") == "function_call" for item in items):
                for item in items:
                    if item.get("type") == "reasoning":
                        for key in ("content", "encrypted_content"):
                            if item.get(key):
                                pending_reasoning.append((key, item[key]))
        else:
            blocks = body.get("content", [])
            if any(item.get("type") == "tool_use" for item in blocks):
                for block in blocks:
                    if block.get("type") == "thinking":
                        for key in ("thinking", "signature"):
                            if block.get(key):
                                pending_reasoning.append((key, block[key]))
        if case == "disconnect" and response.is_success:
            # The real server response arrived but the application cannot parse it.
            # The adapter has max_retries=0 and must not dispatch any local effect.
            raise httpx.ReadError(
                "controlled test disconnect after server response", request=response.request
            )

    class Downstream(StubAgentBackend):
        def __init__(self):
            self.log = []
            self.first_useful = None
            self.files = {"numbers.json": {"values": [7, 11]}}

        def definitions(self, runtime, **kwargs):
            return definitions

        def parallel_safe(self, name, runtime):
            return name == "workspace_read"

        def is_side_effecting(self, name, arguments, runtime):
            return name != "workspace_read"

        @staticmethod
        def path(value):
            return value.removeprefix("/workspace/") if isinstance(value, str) else None

        async def execute_call(self, invocation):
            name = invocation.call.function.name
            args = json.loads(invocation.call.function.arguments)
            if name not in {"workspace_read", "workspace_write"}:
                return '{"ok":false,"executed":false,"error":"isolated_test_capability_denied"}'
            self.log.append((name, args, invocation.identity.operation_id))
            if name == "workspace_read":
                data = self.files.get(self.path(args.get("path")))
                if data is None:
                    return '{"ok":false,"error":"not_found"}'
                return json.dumps({"ok": True, "data": data})
            self.files[self.path(args.get("path"))] = {"text": args.get("text")}
            self.first_useful = time.perf_counter() - started
            return json.dumps({"ok": True, "mutation_committed": True, "data": args})

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            return "test budget exhausted"

    kind = {
        "chat_completions": OpenAICompatibleProvider,
        "responses": DeepSeekResponsesProvider,
        "anthropic_messages": AnthropicMessagesProvider,
    }[protocol]

    class Observed(kind):
        async def complete(self, request):
            result = await super().complete(request)
            responses.append(
                {
                    "status": result.status.value,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "cached_prompt_tokens": result.cached_prompt_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                    "reasoning_present": bool(result.reasoning_content),
                    "continuation_present": result.continuation is not None,
                    "tool_names": [c.function.name for c in result.tool_calls],
                }
            )
            return result

    base = CREDENTIALS[
        "base_url (anthropic)" if protocol == "anthropic_messages" else "base_url (openai)"
    ]
    # Anthropic-compatible endpoints follow the public /anthropic/v1/messages route.
    if protocol == "anthropic_messages":
        base = base.rstrip("/") + "/v1/"
    else:
        base = base.rstrip("/") + "/"
    backend = Downstream()
    error_category = None
    async with httpx.AsyncClient(
        base_url=base,
        timeout=60,
        event_hooks={"request": [request_hook], "response": [response_hook]},
    ) as client:
        adapter = Observed(
            base_url=base,
            api_key=CREDENTIALS["api_key"],
            timeout_seconds=60,
            max_retries=0,
            client=client,
            **({"provider_name": "deepseek"} if protocol == "chat_completions" else {}),
        )
        profile = ModelProfile(
            id="isolated-real-deepseek",
            provider="anthropic" if protocol == "anthropic_messages" else "deepseek",
            protocol=ModelProtocol(protocol),
            base_url=base,
            api_key_env="UNUSED",
            model=CREDENTIALS["model"],
            timeout_seconds=60,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=MAX_OUTPUT,
            reasoning_effort=ReasoningEffort.LOW,
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
        config = replace(
            runtime.runtime_config,
            llm=replace(
                runtime.runtime_config.llm,
                max_output_tokens=1 if case == "truncation" else MAX_OUTPUT,
                thinking_enabled=True,
            ),
        )
        runtime = replace(
            runtime,
            fixed_tools=definitions,
            runtime_config=config,
            max_tool_calls=4,
            max_model_requests=1 if case in {"truncation", "disconnect"} else 4,
            canonical_conversation_id=control.lease.conversation_id,
        )
        program = (
            "r = await yuki_workspace_read({'path':'numbers.json'})\n"
            "n = sum(r['data']['values'])\n"
            "await yuki_workspace_write({'path':'result.txt','text':str(n)})\n"
            "n"
        )
        instruction = (
            "This is an isolated integration test. All business effects use fake downstreams. "
            "The Work is already accepted. Do not call send_message, memory or any other tools. "
            "Read numbers.json with workspace_read; the result has data.values. Add the values and "
            "write their sum as text to result.txt with workspace_write. "
            "The workspace_write receipt includes the committed text. Verify that receipt, "
            "then return exactly TASK_OK_18 immediately. Do not read the file again. "
            + (
                "Use only direct workspace_read/workspace_write calls. Never execute_code."
                if case != "code"
                else "Use exactly one execute_code call with this program:\n" + program
            )
        )
        try:
            result = await runner.run(
                (ChatMessage("system", instruction), ChatMessage("user", "Run the isolated test.")),
                runtime,
                backend,
            )
            names = [name for name, _, _ in backend.log]
            if case in {"direct", "code"}:
                passed = (
                    result.text.strip() == "TASK_OK_18"
                    and names == ["workspace_read", "workspace_write"]
                    and backend.log[-1][1].get("text") == "18"
                    and backend.path(backend.log[-1][1].get("path")) == "result.txt"
                    and backend.files.get("result.txt") == {"text": "18"}
                )
                if case == "code":
                    passed = passed and any("execute_code" in r["tool_names"] for r in responses)
                else:
                    passed = passed and all(
                        "execute_code" not in r["tool_names"] for r in responses
                    )
            elif case == "truncation":
                passed = not backend.log and any(
                    r.get("finish_reason") == "length"
                    or r.get("response_status") == "incomplete"
                    or r.get("stop_reason") == "max_tokens"
                    for r in wire
                )
            else:
                passed = (
                    not backend.log
                    and len(wire) == 1
                    and not responses
                    and bool(result.suppress_delivery)
                )
        except Exception as exc:
            error_category, passed = type(exc).__name__, False
        row = await repo.get(control.current["id"])
        RECORDS.append(
            {
                "protocol": protocol,
                "case": case,
                "passed": passed,
                "error_category": error_category,
                "model": CREDENTIALS["model"],
                "fixed_tools": len(definitions),
                "physical_http": len(wire),
                "normalized_responses": responses,
                "wire": wire,
                "business_calls": len(backend.log),
                "business_names": [name for name, _, _ in backend.log],
                "duplicates": len(backend.log) - len({identity for _, _, identity in backend.log}),
                "logical_request_budget_used": row["model_requests"],
                "business_budget_used": row["tool_calls"],
                "application_usage_unknown_calls": len(wire) - len(responses),
                "first_useful_artifact_seconds": backend.first_useful,
                "total_seconds": time.perf_counter() - started,
                "reasoning_replay_checks": replay_checks,
                "signed_material_observed": any(
                    r["field"] in {"signature", "encrypted_content"} for r in replay_checks
                ),
                "reasoning_continuation_observed": any(
                    r["reasoning_present"] or r["continuation_present"] for r in responses
                ),
                "request_shapes_fixed": len(
                    {json.dumps(json.loads(p).get("tools", []), sort_keys=True) for p in payloads}
                )
                <= 1,
                "tool_payload_sha256": [
                    hashlib.sha256(
                        json.dumps(json.loads(p).get("tools", []), sort_keys=True).encode()
                    ).hexdigest()
                    for p in payloads
                ],
                "real_business_effects": 0,
            }
        )
    await repo.release(control.lease)
    # Only category/booleans appear in failures; never include credentials or raw responses.
    assert passed, f"Provider acceptance failed: {protocol}/{case}, category={error_category}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorize-paid", action="store_true", required=True)
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument("--maximum-total-physical-calls", type=int, choices=(24, 48), default=24)
    parser.add_argument("--case", action="append", dest="selected_cases")
    args = parser.parse_args()
    started_at = datetime.now(UTC).isoformat()
    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    global PHYSICAL_LIMIT, PHYSICAL_CALLS, RESERVED_USD
    PHYSICAL_LIMIT = args.maximum_total_physical_calls
    if args.resume_report:
        previous = json.loads(args.resume_report.read_text(encoding="utf-8"))
        PHYSICAL_CALLS, RESERVED_USD = resume_budget(previous)
    protocols = ("chat_completions", "responses", "anthropic_messages")
    cases = ("direct", "code", "truncation", "disconnect")
    selected = [(p, c) for c in cases for p in protocols]
    if args.selected_cases:
        selection = {tuple(value.split("/")) for value in args.selected_cases}
        if not selection <= set(selected):
            parser.error("unknown protocol/case selection")
        selected = [pair for pair in selected if pair in selection]
    if args.resume_report:
        unresolved = {
            (row["protocol"], row["case"]) for row in previous["records"] if not row["passed"]
        }
        if not set(selected) <= unresolved:
            parser.error("resume may only select failed or unrun cases")
    if not Path(os.environ.get("YUKI_MONTY_BINARY", "")).is_file():
        parser.error("a real Monty binary is required")
    CREDENTIALS.update(read_credentials(args.credentials))
    logging.disable(logging.CRITICAL)
    sys.modules["scripts.verify_deepseek_codemode"] = sys.modules[__name__]
    with tempfile.TemporaryDirectory(prefix="yuki-real-provider-") as temporary:
        path = Path(temporary) / "test_provider.py"
        path.write_text(
            """import pytest
from scripts.verify_deepseek_codemode import provider_case
@pytest.mark.parametrize("protocol,case", """
            + repr(selected)
            + """)
async def test_provider(database, tmp_path, protocol, case):
    await provider_case(database, tmp_path, protocol, case)
"""
        )
        code = pytest.main(
            [
                "-c",
                str(ROOT / "pyproject.toml"),
                "-q",
                "--tb=short",
                "-p",
                "no:warnings",
                "-p",
                "tests.conftest",
                str(path),
            ]
        )
    args.output.write_text(
        json.dumps(
            {
                "model": CREDENTIALS["model"],
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "harness_sha256": harness_sha256,
                "native_worker_sha256": hashlib.sha256(
                    Path(os.environ["YUKI_MONTY_BINARY"]).read_bytes()
                ).hexdigest(),
                "tool_shape_comparison": "complete serialized tools, including schemas/order",
                "exit_code": int(code),
                "records": RECORDS,
                "resume_report": str(args.resume_report) if args.resume_report else None,
                "maximum_physical_calls": PHYSICAL_LIMIT,
                "actual_physical_calls": PHYSICAL_CALLS,
                "cost_ceiling_usd": 1.0,
                "conservative_reserved_usd": RESERVED_USD,
                "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
                "price_basis": "peak Flash rates per million: cache miss input $0.30, "
                "output $1.20; not invoice",
                "scope": "real paid Provider, full fixed declarations, real Runner/SQLite/Monty, "
                "fake business downstream",
                "real_sends": 0,
                "production_access": False,
                "deployment": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    CREDENTIALS.clear()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
