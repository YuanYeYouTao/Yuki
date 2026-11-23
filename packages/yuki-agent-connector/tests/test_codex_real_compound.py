"""Compound commands under a narrowed scope on a real Codex app-server (#647 D1).

Real Codex sends a command approval as ``/bin/zsh -lc '<script>'``. Failure cases
written before the run:
- R1 ``cd <write path> && ls``, run with escalated permissions so Codex asks, is
  reported (the wrapper or the compound script was not judged by its segments);
- R2 ``cd <outside> && ls`` is auto-approved (a ``cd`` out of ``write_paths`` passed).

The app-server, CodexDriver and the declining loop come from test_codex_real_scope.py
(#720): a loopback app-server with a separate CODEX_HOME; the user's home, daemon and
threads are never touched. Every reported request is declined. Run (a few short paid
model turns):
    YUKI_REAL_CODEX_HOME=<a CODEX_HOME holding only auth.json> \\
    YUKI_REAL_CODEX_BINARY=<codex executable> pytest tests/test_codex_real_compound.py
Artifact: test-results/scope-compound/codex-real-<platform>.json.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_codex_real_scope import app_server, until_done, yuki

pytestmark = pytest.mark.skipif(
    not os.environ.get("YUKI_REAL_CODEX_HOME") or not os.environ.get("YUKI_REAL_CODEX_BINARY"),
    reason="paid real Codex run; set YUKI_REAL_CODEX_HOME and YUKI_REAL_CODEX_BINARY",
)
ARTIFACT = Path(__file__).resolve().parents[3] / "test-results" / "scope-compound"


async def test_real_codex_judges_wrapped_compound_commands(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    judged: list[dict[str, Any]] = []
    inside_command = f"cd {project} && ls"
    outside_command = f"cd {outside} && ls"
    async with app_server(project) as endpoint:
        driver, events, _ = await yuki(endpoint, project)
        judge = driver._inside

        def spy(method: str, params: dict[str, Any], scope: Any, thread: str) -> bool:
            inside = judge(method, params, scope, thread)
            judged.append({"method": method, "command": params.get("command"), "inside": inside})
            return inside

        driver._inside = spy  # type: ignore[method-assign]
        try:
            await driver.execute(
                {
                    "id": "compound",
                    "action": "prompt",
                    "arguments": {
                        "cwd": str(project),
                        "scope": {
                            "write_paths": [str(project)],
                            "commands": ["ls"],
                            "network": False,
                        },
                        "prompt": (
                            "Run these two shell commands as two separate tool calls, each "
                            "exactly as written and each with escalated sandbox permissions "
                            "(sandbox_permissions require_escalated, justification: test):\n"
                            f"1. {inside_command}\n2. {outside_command}\n"
                            "If one is denied, do not retry or work around it. Reply done."
                        ),
                    },
                }
            )
            reported = [
                item.get("command") for item in await until_done(driver, events, "compound")
            ]
        finally:
            await driver.stop()
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / f"codex-real-{sys.platform}.json").write_text(
        json.dumps(
            {
                "codex": os.environ["YUKI_REAL_CODEX_BINARY"],
                "judged": judged,
                "reported": reported,
            },
            indent=2,
        )
        + "\n"
    )
    commands = [item for item in judged if item["method"].startswith("item/commandExecution")]
    assert any(item["inside"] and inside_command in str(item["command"]) for item in commands)
    assert any(outside_command in str(command) for command in reported)
    assert not any(inside_command in str(command) for command in reported)
