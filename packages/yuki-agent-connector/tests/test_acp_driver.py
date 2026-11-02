from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers.acp import ACPDriver


@pytest.mark.asyncio
async def test_real_acp_stdio_permissions_local_files_terminal_and_negotiation(
    tmp_path: Path,
) -> None:
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.put_nowait((kind, payload))

    driver = ACPDriver(
        Instance(
            "local-acp",
            "acp",
            "ACP",
            sys.executable,
            args=[str(Path(__file__).parent / "fakes" / "acp_agent.py")],
            cwd=str(tmp_path),
        ),
        emit,
    )
    await asyncio.wait_for(driver.start(), 5)
    try:
        listed = await driver.execute({"id": "readonly", "action": "list", "arguments": {}})
        assert listed["sessions"][0]["cwd"] == str(tmp_path)
        assert not (tmp_path / "written-by-actual-client.txt").exists()
        prompt = await asyncio.wait_for(
            driver.execute(
                {
                    "id": "original-op",
                    "action": "prompt",
                    "arguments": {"prompt": "exercise callbacks"},
                }
            ),
            5,
        )
        assert prompt["admitted"] is True and prompt["execution_id"]
        kind, progress = await asyncio.wait_for(events.get(), 3)
        assert kind == "progress" and progress["operation_id"] == "original-op"
        kind, permission = await asyncio.wait_for(events.get(), 3)
        assert kind == "permission" and permission["execution_id"] == prompt["execution_id"]
        assert permission["payload"]["native_request_id"] == 11
        with pytest.raises(DriverError, match="original native option"):
            await driver.execute(
                {
                    "id": "bad-answer",
                    "action": "respond",
                    "session_id": prompt["session_id"],
                    "execution_id": prompt["execution_id"],
                    "arguments": {
                        "connection_id": driver.owner,
                        "permission_id": "11",
                        "option_id": "made-up",
                    },
                }
            )
        await driver.execute(
            {
                "id": "answer",
                "action": "respond",
                "session_id": prompt["session_id"],
                "execution_id": prompt["execution_id"],
                "arguments": {
                    "connection_id": driver.owner,
                    "permission_id": 11,
                    "option_id": "allow-original",
                },
            }
        )
        kind, completed = await asyncio.wait_for(events.get(), 5)
        assert kind == "completed" and completed["payload"]["text"] == "LOCAL_RESULT"
        kind, finished = await asyncio.wait_for(events.get(), 5)
        assert kind == "work_finished" and finished["operation_id"] == "original-op"
        await driver.sessions[prompt["session_id"]].task
        assert (tmp_path / "written-by-actual-client.txt").read_text() == "LOCAL_FILESYSTEM"
        assert not driver.terminals
        assert not driver.requests
        second = await asyncio.wait_for(
            driver.execute(
                {
                    "id": "detached-op",
                    "action": "prompt",
                    "session_id": prompt["session_id"],
                    "arguments": {"prompt": "continue"},
                }
            ),
            5,
        )
        assert (await asyncio.wait_for(events.get(), 3))[0] == "progress"
        kind, permission = await asyncio.wait_for(events.get(), 3)
        assert kind == "permission" and permission["operation_id"] == "detached-op"
        await driver.execute(
            {
                "id": "detach-work",
                "action": "detach",
                "session_id": second["session_id"],
                "arguments": {"operation_id": "detached-op"},
            }
        )
        await driver.execute(
            {
                "id": "answer-detached",
                "action": "respond",
                "session_id": second["session_id"],
                "execution_id": second["execution_id"],
                "arguments": {
                    "connection_id": driver.owner,
                    "permission_id": permission["payload"]["native_request_id"],
                    "option_id": "allow-original",
                },
            }
        )
        kind, finished = await asyncio.wait_for(events.get(), 5)
        assert kind == "work_finished" and finished["operation_id"] == "detached-op"
        with pytest.raises(DriverError, match="precise steer/queue"):
            await driver.execute({"id": "queue", "action": "queue", "arguments": {}})
        await driver.execute(
            {"id": "watch", "action": "watch", "session_id": prompt["session_id"], "arguments": {}}
        )
        await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": prompt["session_id"],
                "arguments": {"operation_id": "watch"},
            }
        )
        assert (await driver.execute({"id": "again", "action": "list", "arguments": {}}))[
            "sessions"
        ]
    finally:
        await asyncio.wait_for(driver.stop(), 5)


@pytest.mark.asyncio
async def test_optional_acp_session_directory_is_not_fabricated(tmp_path: Path) -> None:
    async def emit(kind: str, payload: dict[str, Any]) -> None:
        pass

    driver = ACPDriver(
        Instance(
            "local-acp",
            "acp",
            "ACP",
            sys.executable,
            args=[str(Path(__file__).parent / "fakes" / "acp_agent.py")],
            cwd=str(tmp_path),
            env={"WITHOUT_SESSION_LIST": "1"},
        ),
        emit,
    )
    await asyncio.wait_for(driver.start(), 5)
    try:
        assert "list" not in driver.capabilities
        with pytest.raises(DriverError, match="does not advertise session/list"):
            await driver.execute({"id": "list", "action": "list", "arguments": {}})
    finally:
        await asyncio.wait_for(driver.stop(), 5)


@pytest.mark.asyncio
async def test_incompatible_acp_version_is_not_reported_as_ready(tmp_path: Path) -> None:
    async def emit(kind: str, payload: dict[str, Any]) -> None:
        pass

    driver = ACPDriver(
        Instance(
            "original",
            "acp",
            "ACP",
            sys.executable,
            args=[str(Path(__file__).parent / "fakes" / "acp_agent.py")],
            cwd=str(tmp_path),
            env={"FAKE_ACP_VERSION": "2"},
        ),
        emit,
    )
    try:
        with pytest.raises(DriverError) as error:
            await asyncio.wait_for(driver.start(), 5)
        assert error.value.code == "native_protocol_unsupported"
    finally:
        await asyncio.wait_for(driver.stop(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_acp_auth_refusal_before_prompt_is_not_an_unknown_execution(
    tmp_path: Path, resume: bool
) -> None:
    trace = tmp_path / "acp-methods.txt"

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        pass

    driver = ACPDriver(
        Instance(
            "original",
            "acp",
            "ACP",
            sys.executable,
            args=[str(Path(__file__).parent / "fakes" / "acp_agent.py")],
            cwd=str(tmp_path),
            env={
                "FAKE_ACP_AUTH_REQUIRED": "1",
                "FAKE_ACP_LIST_AUTH_REQUIRED": "1" if resume else "",
                "FAKE_ACP_TRACE": str(trace),
            },
        ),
        emit,
    )
    await asyncio.wait_for(driver.start(), 5)
    try:
        with pytest.raises(DriverError) as error:
            await driver.execute(
                {
                    "id": "original",
                    "action": "prompt",
                    "session_id": "native-session" if resume else None,
                    "arguments": {"prompt": "unused", "resume": resume},
                }
            )
        assert error.value.code == "native_login_required" and not error.value.uncertain
        assert "fixture-secret" not in str(error.value)
    finally:
        await asyncio.wait_for(driver.stop(), 5)
    assert trace.read_text().splitlines() == [
        "initialize",
        "session/list" if resume else "session/new",
    ]
