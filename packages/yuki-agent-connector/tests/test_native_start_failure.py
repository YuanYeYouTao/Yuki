"""A native agent that never started reports a definite failure, not an unknown one.

Production 2026-11-20: the Claude SDK refused to start the CLI (CLIConnectionError) and
the connector reported the delegation's start as ``unknown`` (``native_result_not_
confirmed``). Nothing had been sent to Claude, yet Yuki could only wait.

Failure cases written before the change:
- F1: a Claude session whose SDK never connects is reported as uncertain;
- F2: the failure hides why (the SDK's own message and error type are lost);
- F3: the broken session stays registered, so the next input for it is refused or hangs;
- F4: a failure after the SDK connected is reported as definite (it may have run).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import test_claude_phase2 as claude
from claude_agent_sdk import CLIConnectionError

from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers import claude as module


class _NeverConnects:
    def __init__(self, *, options: Any) -> None:
        self.options = options

    async def __aenter__(self) -> _NeverConnects:
        raise CLIConnectionError("Working directory does not exist: /nowhere")

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_a_claude_cli_that_never_started_is_a_definite_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, configurations, events = await claude.started(tmp_path, monkeypatch)
    working = module.ClaudeSDKClient
    monkeypatch.setattr(module, "ClaudeSDKClient", _NeverConnects)
    command = {"id": "first", "action": "prompt", "arguments": {"prompt": "go"}}
    try:
        with pytest.raises(DriverError) as failed:
            await driver.execute(command)
        assert failed.value.code == "native_start_failed"
        assert failed.value.uncertain is False
        assert failed.value.details == {"error_type": "CLIConnectionError"}
        assert "Working directory does not exist" in str(failed.value)
        assert driver.sessions == {}
        # Nothing is left behind: the next delegation starts normally.
        monkeypatch.setattr(module, "ClaudeSDKClient", working)
        await driver.execute({**command, "id": "second"})
        await claude.until(events, lambda kind, value: value["operation_id"] == "second")
        assert len(configurations) == 1
    finally:
        await driver.stop()
