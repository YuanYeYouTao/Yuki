"""ACP client callbacks use only absolute paths (connector 0.9.6).

Failure cases written before the implementation:
- A1: ``fs/write_text_file`` with a relative ``path`` is judged against the session's
  cwd but written relative to the connector process's cwd (outside the scope);
- A2: ``fs/read_text_file`` with a relative ``path`` reads a file relative to the
  connector process's cwd;
- A3: ``terminal/create`` with a relative ``cwd`` starts the command in a directory
  relative to the connector process;
- A4: an absent ``cwd`` (the session's cwd), or absolute paths, stop working.

ACP 1.0 requires these paths to be absolute, so a relative one is refused, scoped or
not; the path a scope judges is then the path that is used.

The started command reports its working directory through this Python, not ``pwd``:
on Windows the ``pwd`` found on PATH is Git's MSYS one, which prints ``/c/...``.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
import test_acp_phase2 as acp


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [True, False])
async def test_acp_callbacks_refuse_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scoped: bool
) -> None:
    work = tmp_path / "work"
    # ``sub`` exists under the session cwd too: a relative cwd wrongly joined to it
    # would start there and report it, instead of failing on a missing directory.
    (work / "sub").mkdir(parents=True)
    # The connector process's own cwd, outside the session and its scope.
    process = tmp_path / "process"
    (process / "sub").mkdir(parents=True)
    (process / "existing.txt").write_text("connector-side")
    monkeypatch.chdir(process)
    # Outside the session cwd and the connector's cwd: prints where it was started.
    where = tmp_path / "where.py"
    where.write_text("import os\nprint(os.getcwd())\n")
    probe = {"terminal": sys.executable, "args": [str(where)], "output": True}
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_MODES=acp.CLAUDE_MODES)
    script = {
        "during": [
            {"write": "rel.txt"},
            {"write": str(work / "abs.txt")},
            {"read": "existing.txt"},
            {"read": str(work / "abs.txt")},
            {**probe, "cwd": "sub"},
            probe,
            {**probe, "cwd": str(tmp_path)},
        ]
    }
    commands = [shlex.join([sys.executable])]
    scope = {"scope": {"write_paths": [str(work)], "commands": commands}} if scoped else {}
    try:
        await driver.execute(acp.prompt("callbacks", script, cwd=str(work), **scope))
        await acp.until(
            events,
            lambda kind, value: kind == "work_finished" and value["operation_id"] == "callbacks",
        )
        observed = {
            "writes": [
                (row["path"], row["error"]) for row in acp.traced(trace, "fs/write_text_file")
            ],
            "written_relative": (process / "rel.txt").exists() or (work / "rel.txt").exists(),
            "reads": [
                (row["path"], row["error"]) for row in acp.traced(trace, "fs/read_text_file")
            ],
            # Where each command really started (resolved; case-insensitive on Windows).
            "terminals": [
                (row["cwd"], row["error"], output and Path(output).resolve())  # noqa: ASYNC240
                for row in acp.traced(trace, "terminal/create")
                for output in [row.get("output", "").strip()]
            ],
        }
        assert observed == {
            "writes": [("rel.txt", True), (str(work / "abs.txt"), False)],
            "written_relative": False,
            "reads": [("existing.txt", True), (str(work / "abs.txt"), False)],
            "terminals": [
                ("sub", True, ""),
                (None, False, work.resolve()),
                (str(tmp_path), False, tmp_path.resolve()),  # noqa: ASYNC240
            ],
        }
    finally:
        await driver.stop()
