"""A detached ACP delegation's scope stays with its session (connector 0.9.7).

Failure cases written before the implementation:
- D1: Yuki detaches (with ``release``) a scoped delegation while its prompt is still
  running; the session closes only after the turn, and in between an
  ``fs/write_text_file`` outside the scope is written;
- D2: in the same window a ``terminal/create`` outside the scope starts;
- D3: detached without ``release`` the session stays loaded; the rest of the prompt
  and a later agent-initiated (now unowned) turn write and start outside the scope;
- D4: callbacks inside the scope stop working after the detach (the session is
  refused wholesale instead of kept to its scope);
- D5: a later scoped prompt on the still-loaded session is judged by the old scope
  (scopes stack) instead of its own;
- D6: ``release`` no longer closes the session after the turn;
- D7: the retained scope auto-approves a permission request of the detached turn
  (only a delegation owning the turn may; with no watch left it is deny-only).

Artifact: test-results/connector-stability/acp-detach-scope-<release>-<platform>.json
(the trace of every callback and whether it ran).
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import test_acp_phase2 as acp

ARTIFACTS = Path(__file__).resolve().parents[3] / "test-results" / "connector-stability"


def _probe(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A script that leaves a marker file when it runs."""
    marker = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n")
    return script, marker


def _steps(work: Path, outside: Path, inside: Path, escape: Path, tag: str) -> list[Any]:
    return [
        {"write": str(work / f"{tag}-in.txt")},
        {"write": str(outside / f"{tag}-out.txt")},
        # Released by the agent after use, as real ACP agents do.
        {"terminal": sys.executable, "args": [str(inside)], "output": True, "release": True},
        {"terminal": sys.executable, "args": [str(escape)], "output": True, "release": True},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("release", [True, False])
async def test_acp_detach_mid_turn_keeps_the_scope(tmp_path: Path, release: bool) -> None:
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    later = tmp_path / "later"
    for directory in (work, outside, later):
        directory.mkdir()
    inside, inside_ran = _probe(tmp_path, "inside")
    escape, escape_ran = _probe(tmp_path, "escape")
    gate = tmp_path / "detached"
    commands = [shlex.join([sys.executable, str(inside)])]
    script = {
        "during": [
            {"say": "started"},
            {"wait": str(gate)},
            *_steps(work, outside, inside, escape, "during"),
            # Inside the scope: auto-approved while owned, not after the detach.
            acp.permission("edit-in", "edit", locations=[str(work / "edit.py")]),
        ],
        # Released, the session closes after the turn: no agent-initiated turn follows.
        "after": [] if release else _steps(work, outside, inside, escape, "after"),
    }
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_MODES=acp.CLAUDE_MODES)
    try:
        started = await driver.execute(
            acp.prompt(
                "scoped",
                script,
                cwd=str(work),
                scope={"write_paths": [str(work)], "commands": commands},
            )
        )
        session_id = started["session_id"]
        assert started["pending"] is True
        detached = await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "scoped", "release": release},
            }
        )
        gate.write_text("go")
        _, unowned = await acp.until(
            events, lambda kind, value: kind == "permission" and value["payload"].get("unowned")
        )
        await driver.execute(
            {
                "id": "deny",
                "action": "respond",
                "session_id": session_id,
                "arguments": {
                    "connection_id": driver.owner,
                    "permission_id": unowned["payload"]["permission_id"],
                    "option_id": "deny",
                },
            }
        )
        await acp.until(
            events,
            lambda kind, value: kind == "work_finished" and value["operation_id"] == "scoped",
        )
        # Unreleased, the agent-initiated turn writes and starts after the response.
        await acp.traced_until(trace, "fs/write_text_file", 2 if release else 4)
        await acp.traced_until(trace, "terminal/create", 2 if release else 4)
        if release:
            await acp.traced_until(trace, "session/close")
        else:
            # D5: the next scoped prompt on the loaded session is judged by its own scope.
            await driver.execute(
                {
                    **acp.prompt(
                        "second",
                        {
                            "during": [
                                {"write": str(later / "second-in.txt")},
                                {"write": str(work / "second-out.txt")},
                            ]
                        },
                        scope={"write_paths": [str(later)], "commands": commands},
                    ),
                    "session_id": session_id,
                },
            )
            await acp.until(
                events,
                lambda kind, value: kind == "work_finished" and value["operation_id"] == "second",
            )
        observed = {
            "closed": detached["closed"] if release else None,
            "writes": [
                (Path(row["path"]).name, row["error"])
                for row in acp.traced(trace, "fs/write_text_file")
            ],
            "terminals": [row["error"] for row in acp.traced(trace, "terminal/create")],
            "files": sorted(
                str(path.relative_to(tmp_path).as_posix())
                for path in (*work.iterdir(), *outside.iterdir(), *later.iterdir())
            ),
            "escape_ran": escape_ran.exists(),
            "permission": {
                "options": unowned["payload"]["options"],
                "outcome": [
                    row["outcome"] for row in acp.traced(trace, "session/request_permission")
                ],
            },
            "inside_ran": inside_ran.exists(),
            "session_closed": [row["session"] for row in acp.traced(trace, "session/close")],
            "prompted_sessions": [row["session"] for row in acp.traced(trace, "session/prompt")],
        }
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"acp-detach-scope-{release}-{sys.platform}.json").write_text(
            json.dumps({"observed": observed, "trace": acp.traced(trace)}, indent=2)
        )
        assert observed == {
            "closed": "after_turn" if release else None,
            "writes": [
                ("during-in.txt", False),
                ("during-out.txt", True),
                *(
                    []
                    if release
                    else [
                        ("after-in.txt", False),
                        ("after-out.txt", True),
                        ("second-in.txt", False),
                        ("second-out.txt", True),
                    ]
                ),
            ],
            "terminals": [False, True] if release else [False, True, False, True],
            "files": sorted(
                ["work/during-in.txt"]
                if release
                else ["work/during-in.txt", "work/after-in.txt", "later/second-in.txt"]
            ),
            "escape_ran": False,
            "permission": {
                "options": ["deny"],
                "outcome": [{"outcome": "selected", "optionId": "reject"}],
            },
            "inside_ran": True,
            "session_closed": [session_id] if release else [],
            # D5 replaces the scope on the same loaded session, not on a new one.
            "prompted_sessions": [session_id] if release else [session_id, session_id],
        }
    finally:
        await driver.stop()
