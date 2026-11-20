"""A delegation's working directory may be written relative to the user's home.

Production 2026-11-20: Yuki delegated with ``cwd: "~/Developer/personal"``; the Claude
SDK took ``~`` literally and refused to start (``CLIConnectionError: Working directory
does not exist``), so the delegation never ran. Scope paths were already expanded.

Failure cases written before the change:
- H1: a Claude session opened with ``~/…`` is started in a literal ``~`` directory;
- H2: a new Codex thread is started with a literal ``~`` cwd;
- H3: a new ACP session is created with a literal ``~`` cwd;
- H4: an absolute cwd is changed.
Paths are compared as paths: on Windows ``~/work`` expands with a mixed separator.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import test_acp_phase2 as acp
import test_claude_phase2 as claude
import test_codex_phase2 as codex
from websockets.asyncio.server import serve


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["~/work", "absolute"])
async def test_claude_opens_in_the_expanded_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path, given: str
) -> None:
    driver, _, configurations, events = await claude.started(tmp_path, monkeypatch)
    cwd = given if given != "absolute" else str(home / "work")
    try:
        await driver.execute(
            {"id": "home", "action": "prompt", "arguments": {"prompt": "go", "cwd": cwd}}
        )
        await claude.until(events, lambda kind, value: value["operation_id"] == "home")
        assert Path(configurations[0].cwd) == home / "work"
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_codex_starts_a_new_thread_in_the_expanded_home_directory(
    tmp_path: Path, home: Path
) -> None:
    owner = codex.AppServer(cwd=str(home / "work"))
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _ = await codex.connected(owner, listener, tmp_path)
        try:
            await driver.execute(
                {
                    "id": "home",
                    "action": "prompt",
                    "session_id": None,
                    "execution_id": None,
                    "arguments": {"prompt": "fix", "cwd": "~/work"},
                }
            )
            started = next(c for c in owner.calls if c.get("method") == "thread/start")
            assert Path(started["params"]["cwd"]) == home / "work"
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_acp_creates_a_session_in_the_expanded_home_directory(
    tmp_path: Path, home: Path
) -> None:
    driver, _, _ = await acp.started(tmp_path)
    try:
        await driver.execute(acp.prompt("home", {"during": []}, cwd="~/work"))
        [session] = driver.sessions.values()
        assert Path(session.cwd) == home / "work"
    finally:
        await driver.stop()
