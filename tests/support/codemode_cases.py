"""Code Mode fixtures: real Work/effects/InvocationService and the real worker.

The fake business backend writes an independent external log; effect rows are
never used to prove that a call did or did not happen downstream.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.contract import EXECUTE_CODE_TOOL
from qq_ai_bot.codemode.driver import ChildClass, CodeHost, CodeModeDriver
from qq_ai_bot.codemode.engine_monty import PinnedWorker
from qq_ai_bot.codemode.limits import CodeModeLimits
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.services.invocation_service import InvocationService
from tests.unit.test_tool_effect_audit import active_work

BINARY = Path(os.environ.get("YUKI_MONTY_BINARY", "/nonexistent/monty"))
try:
    import pydantic_monty  # noqa: F401

    BINDING = True
except ImportError:
    BINDING = False

requires_worker = pytest.mark.skipif(
    not (BINDING and BINARY.is_file()),
    reason="pinned Monty worker/binding not built; set YUKI_MONTY_BINARY "
    "(scripts/build_monty_worker.sh)",
)

READS = ("lookup", "search_chat_history")
WRITES = ("workspace_write",)
TOOLS = (
    *(ChatTool(name, name, {"type": "object"}) for name in (*READS, *WRITES)),
    ChatTool("send_message", "send", {"type": "object"}),
    ChatTool("memory_change", "memory", {"type": "object"}),
    ChatTool("task_control", "control", {"type": "object"}),
    EXECUTE_CODE_TOOL,
)
LIMITS = CodeModeLimits(
    max_feed_seconds=2.0, max_memory_bytes=32 << 20, request_timeout_seconds=10.0
)


def worker() -> PinnedWorker:
    # Linux acceptance uses the actual root-owned namespace launcher. Leaving
    # it unset still exercises the production refusal; there is no unrestricted
    # Linux fallback in the fixture or in the engine.
    value = os.environ.get("YUKI_MONTY_LAUNCHER")
    launcher = Path(value) if value else None
    return PinnedWorker(
        BINARY,
        hashlib.sha256(BINARY.read_bytes()).hexdigest(),
        launcher,
        hashlib.sha256(launcher.read_bytes()).hexdigest() if launcher else "",
    )


@dataclass
class FakeDomain:
    """Downstream with its own append-only log and scripted per-tool replies."""

    log: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    replies: dict[str, Any] = field(default_factory=dict)
    denied: set[str] = field(default_factory=set)
    inflight: int = 0
    peak: int = 0

    async def __call__(self, name: str, arguments: str) -> str:
        import asyncio

        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)
            args = json.loads(arguments)
            if name in self.denied:
                return json.dumps(
                    {"ok": False, "executed": False, "error": "capability_no_longer_authorized"}
                )
            self.log.append((name, args))
            reply = self.replies.get(name)
            if callable(reply):
                reply = reply(args)
            return json.dumps(reply if reply is not None else {"ok": True, "data": args})
        finally:
            self.inflight -= 1


def classify(call: ToolCall) -> ChildClass:
    name = call.function.name
    if name == "task_control":
        return ChildClass("control", False, False)
    if name == "memory_change":
        return ChildClass("memory_write", False, True)
    if name == "send_message":
        return ChildClass("send", False, True)
    if name in WRITES:
        return ChildClass("write", False, True)
    return ChildClass("read", True, False)


async def environment(database, tmp_path, *, tool_limit=32, max_parallel=2, reporting=None):
    _env, owner, _runtime = await active_work(database, tmp_path, reporting=reporting)
    owner.control.session = owner
    owner.transcript.append(ChatMessage("user", "compose"))
    return build_host(owner, FakeDomain(), tool_limit=tool_limit, max_parallel=max_parallel)


def build_host(owner, domain, *, tool_limit=32, max_parallel=2):
    control = owner.control
    controls: list[tuple[str, str]] = []
    service = InvocationService()
    agent = SimpleNamespace(work_control=control)

    async def execute_business(invocation, side_effecting):
        async def invoke():
            return await domain(invocation.call.function.name, invocation.call.function.arguments)

        return await service.invoke(invocation, invoke, side_effecting=side_effecting)

    async def execute_control(call, key):
        controls.append((call.function.arguments, key))
        arguments = json.loads(call.function.arguments)
        return await control.execute(call.function.name, arguments, key), True

    async def before_dispatch(_call):
        return None

    api = project(TOOLS, "manifest-test")
    host = CodeHost(
        control=control,
        api=api,
        worker=worker() if BINARY.is_file() else None,
        limits=LIMITS,
        execute_business=execute_business,
        execute_control=execute_control,
        before_dispatch=before_dispatch,
        classify=classify,
        max_parallel=max_parallel,
        tool_limit=tool_limit,
        result_limit=8000,
    )
    return SimpleNamespace(
        owner=owner, control=control, domain=domain, host=host, agent=agent, controls=controls
    )


def outer_call(env, code, *, call_id="code-1", inputs=None):
    call = ToolCall(
        call_id,
        ToolFunction("execute_code", json.dumps({"code": code, "inputs": inputs or {}})),
    )
    return direct_invocations((call,), env.agent, manifest_revision="manifest-test")[0]


async def run_code(env, code, **kwargs):
    outer = outer_call(env, code, **kwargs)
    result = await CodeModeDriver(env.host, outer).run()
    return json.loads(result), outer


async def effect_rows(database, work_id):
    async with database.sessions() as reader:
        rows = (
            (await reader.execute(select(effects).where(effects.c.work_id == work_id)))
            .mappings()
            .all()
        )
        tools = await reader.scalar(select(work.c.tool_calls).where(work.c.id == work_id))
        root = await reader.scalar(select(budgets.c.tools).where(budgets.c.root_id == work_id))
    return {row["effect_key"]: dict(row) for row in rows}, tools or 0, root or 0
