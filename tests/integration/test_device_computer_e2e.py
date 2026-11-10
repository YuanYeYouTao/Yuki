"""``computer``/``browser`` with ``device``: the model tool → LocalDeviceService → TLS WSS →
an actual Connector process (its journal, stop flag and file transfers) and back.

Failure cases: docs/operations/computer-browser-device-3a-20261110.md §1 — routing and
facts (D4, G2), full access (D5), image transfer and media failures (G1, D6, M6), stop
and resume through the tool (D8, D9 including resume by someone else), retries and
interruption (C4, C5). Only the operating system's screen is replaced, by
packages/yuki-agent-connector/tests/fakes/screen_connector.py.
Artifact: test-results/device-computer/server-<test>.json.
"""

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from tests.conftest import make_settings
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_local_device_transport import actual_connector
from tests.support.canonical_identity import ensure_person
from tests.support.service_dependencies import tool_dependencies
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.screen import Display, frame, to_global

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.capabilities.results import ToolResultBudgeter
from yuki.client_api.errors import ClientError
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.agent_tools import AgentToolService, ToolRuntime

ROOT = Path(__file__).resolve().parents[2]
CONNECTOR = ROOT / "packages/yuki-agent-connector"
FAKE = CONNECTOR / "tests/fakes/screen_connector.py"
ARTIFACTS = ROOT / "test-results" / "device-computer"
RETINA = {
    "id": "1",
    "x": 0,
    "y": 0,
    "width": 1512,
    "height": 982,
    "pixel_width": 3024,
    "pixel_height": 1964,
    "primary": True,
    "scale": 2.0,
}
LIMITS = {"vision_max_dimension": 1568, "vision_max_pixels": 1_500_000}


def script(state: Path, **facts) -> None:
    path = state / "script.json"
    current = json.loads(path.read_text()) if path.exists() else {"displays": [RETINA]}
    path.write_text(json.dumps({**current, "chrome": False, **facts}))


def events(state: Path) -> list[dict]:
    path = state / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [json.loads(line) for line in lines]


class Process:
    """The actual Connector CLI as Desktop runs it, behind the fake screen."""

    def __init__(self, bridge, tmp_path: Path) -> None:
        self.bridge, self.state, self.data = bridge, tmp_path / "state", tmp_path / "data"
        self.state.mkdir(exist_ok=True)
        self.lines: list[dict] = []

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(FAKE),
            str(self.state),
            "--data-dir",
            str(self.data),
            "run",
            "--desktop-stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONPATH": str(CONNECTOR / "src"),
                "SSL_CERT_FILE": str(self.bridge.ca_file),
                "NO_PROXY": "127.0.0.1,localhost,::1",
            },
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self.reader = asyncio.create_task(self._read())
        device = self.bridge.device["device_id"]
        async with asyncio.timeout(30):
            while not self.bridge.service.connections.online(device):  # noqa: ASYNC110
                await asyncio.sleep(0.02)

    async def _read(self) -> None:
        while raw := await self.process.stdout.readline():
            self.lines.append(json.loads(raw))

    def send(self, value: dict) -> None:
        self.process.stdin.write((json.dumps(value) + "\n").encode())

    async def stop(self) -> str:
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 20)
        await self.reader
        return (await self.process.stderr.read()).decode()

    async def kill(self) -> None:
        self.process.kill()
        await self.process.wait()
        await self.reader


def runtime_for(person: str) -> ToolRuntime:
    return ToolRuntime(
        inbound=InboundMessage(
            "device-trigger",
            "device-trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("10001"),
            "看一下我的电脑",
            bot_user_id="80001",
            person_id=person,
        ),
        execution_id="device-execution",
        conversation_id="device-conversation",
        target_presence_id="presence-test",
    )


def tools_for(database, bridge) -> AgentToolService:
    settings = make_settings(database.url, **LIMITS)
    tools = AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        **tool_dependencies(database, settings),
    )
    tools.sandbox_client.local_devices = bridge.service.local_devices
    return tools


async def call(tools, runtime, key: str, name: str, **arguments):
    token = current_invocation.set(ToolInvocationContext(runtime, key))
    try:
        return await tools.execute(name, json.dumps(arguments, ensure_ascii=False), runtime)
    finally:
        current_invocation.reset(token)


def view(result) -> dict:
    data = result.data
    return {
        "ok": result.ok,
        "error": result.error_code,
        "uncertain": result.uncertain,
        "mutation_committed": result.mutation_committed,
        "metadata": result.metadata,
        "images": [len(image.data_url) for image in result.images],
        "data": dict(data) if isinstance(data, dict) else data,
    }


def write(name: str, value: dict) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"server-{name}.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


async def test_device_computer_through_the_model_tool(bridge, database, tmp_path):
    connector = Process(bridge, tmp_path)
    script(connector.state)
    Config(
        bridge.websocket_url, bridge.device["device_id"], bridge.device["token"], full_access=True
    ).save(connector.data / "config.json")
    device = bridge.device["device_id"]
    tools = tools_for(database, bridge)
    owner = runtime_for(bridge.env.person)
    calls: dict[str, dict] = {}
    await connector.start()
    try:
        # D4: get_environment(device) carries the screen facts.
        environment = await call(tools, owner, "env-1", "get_environment", device=device)
        assert environment.ok, environment
        calls["env-1"] = view(environment)
        assert environment.data["computer"] == {
            "screen": True,
            "input": True,
            "displays": [Display(**RETINA).fact()],
        }

        # G1/M6: the image comes back through the download loop at the source's size.
        shot = await call(tools, owner, "shot-1", "computer", action="screenshot", device=device)
        calls["shot-1"] = view(shot)
        assert shot.ok, shot
        size = frame(Display(**RETINA), 1568, 1_500_000)
        assert (shot.data["width"], shot.data["height"]) == size
        assert shot.mutation_committed is None
        (image,) = shot.images
        assert image.source == "tool" and image.data_url.startswith("data:image/jpeg;base64,")
        picture = Image.open(io.BytesIO(base64.b64decode(image.data_url.split(",", 1)[1])))
        assert picture.size == size
        assert not any((connector.data / "captures").iterdir())

        clicked = await call(
            tools, owner, "click-1", "computer", action="click", x=300, y=200, device=device
        )
        calls["click-1"] = view(clicked)
        assert clicked.ok and clicked.mutation_committed is True, clicked
        assert clicked.data["submitted"] is True and len(clicked.images) == 1
        posted = events(connector.state)
        assert len(posted) == 1
        expected = to_global(Display(**RETINA), 1568, 1_500_000, 300, 200)
        assert (posted[0]["x"], posted[0]["y"]) == pytest.approx(expected)

        # C4: the same call key returns the original receipt and clicks nothing; its
        # picture was already read once, so the replay reports media_read:false (D6).
        replay = await call(
            tools, owner, "click-1", "computer", action="click", x=300, y=200, device=device
        )
        calls["click-1-replay"] = view(replay)
        assert replay.ok and replay.data["submitted"] is True
        assert replay.metadata["media_read"] is False and not replay.images
        assert len(events(connector.state)) == 1
        # D6: an archive failure keeps the click; only the pixels are unread.
        rendered = json.loads(
            (await ToolResultBudgeter(max_characters=100_000).render(clicked)).text
        )
        calls["click-1-unarchived"] = rendered
        assert rendered["submitted"] is True and rendered["media_read"] is False

        # D8/D12: the owner uses the computer; the next input waits for the owner to stop,
        # then is refused without running, and is not a mutation or uncertain.
        await asyncio.sleep(0.05)
        (connector.state / "owner_last_input").write_text(str(time.time()))
        yielded = await call(
            tools, owner, "click-2", "computer", action="click", x=1, y=1, device=device
        )
        calls["click-2"] = view(yielded)
        assert yielded.error_code == "owner_active" and not yielded.uncertain, yielded
        assert yielded.mutation_committed is not True
        assert len(events(connector.state)) == 1
        # D11: the owner's input did not stop Yuki: no control line, nothing to resume.
        assert [line["holder"] for line in connector.lines if line["type"] == "control"] == [
            "none"
        ]
        # D14: the model looks again, then the retried click runs.
        read = await call(tools, owner, "shot-2", "computer", action="screenshot", device=device)
        assert read.ok and read.data["control"]["holder"] == "none"
        retried = await call(
            tools, owner, "click-2-retry", "computer", action="click", x=1, y=1, device=device
        )
        calls["click-2-retry"] = view(retried)
        assert retried.ok and retried.data["submitted"] is True, retried
        assert len(events(connector.state)) == 2

        # D9: Desktop 「停止」 refuses computer and browser input; a read is not refused.
        connector.send({"type": "control", "holder": "owner"})
        holders = lambda: [line.get("holder") for line in connector.lines]  # noqa: E731
        async with asyncio.timeout(20):
            while "owner" not in holders():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        held = await call(
            tools, owner, "click-held", "computer", action="click", x=1, y=1, device=device
        )
        calls["click-held"] = view(held)
        assert held.error_code == "control_held" and not held.uncertain
        assert held.data["control"]["holder"] == "owner"
        browser = await call(
            tools, owner, "open-1", "browser", action="open", url="https://example.com",
            device=device,
        )
        calls["open-1"] = view(browser)
        assert browser.error_code == "control_held"
        read = await call(tools, owner, "shot-held", "computer", action="screenshot", device=device)
        assert read.ok and read.data["control"]["holder"] == "owner"
        assert len(events(connector.state)) == 2

        # D9: only the device's person may resume; someone else is told nothing exists.
        async with database.immediate_session() as session:
            stranger = await ensure_person(session, "10003")
        with pytest.raises(ClientError) as refused:
            await call(
                tools, runtime_for(stranger), "resume-x", "computer", action="resume",
                device=device,
            )
        assert refused.value.code == "not_found"
        still = await call(tools, owner, "shot-3", "computer", action="screenshot", device=device)
        assert still.data["control"]["holder"] == "owner"
        resumed = await call(tools, owner, "resume-1", "computer", action="resume", device=device)
        calls["resume-1"] = view(resumed)
        assert resumed.ok and resumed.data["control"]["holder"] == "none"
        assert connector.lines[-1]["type"] == "control"
        assert connector.lines[-1]["reason"] == "resume"
        typed = await call(
            tools, owner, "type-1", "computer", action="type", text="中文 ok", device=device
        )
        assert typed.ok and events(connector.state)[-1] == {"kind": "type", "text": "中文 ok"}

        # 3a has no per-window delivery; a browser without Chrome says so.
        window = await call(
            tools, owner, "window-1", "computer", action="screenshot", window="0x1",
            device=device,
        )
        assert window.error_code == "device_not_supported"
        missing = await call(tools, owner, "snap-1", "browser", action="snapshot", device=device)
        assert missing.error_code == "browser_unavailable"

        # D5: full access off refuses from the next command on.
        Config.update(
            connector.data / "config.json", lambda value: replace(value, full_access=False)
        )
        closed = await call(tools, owner, "shot-4", "computer", action="screenshot", device=device)
        calls["shot-4"] = view(closed)
        assert closed.error_code == "full_access_disabled"
        Config.update(
            connector.data / "config.json", lambda value: replace(value, full_access=True)
        )

        # C5: the Connector dies during an input: the call is unknown, never re-driven.
        script(connector.state, hang=True)
        pending = asyncio.create_task(
            call(tools, owner, "click-3", "computer", action="click", x=5, y=5, device=device)
        )
        async with asyncio.timeout(20):
            while not (connector.state / "hanging").exists():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        await connector.kill()
        lost = await pending
        calls["click-3"] = view(lost)
        assert lost.error_code == "device_offline" and lost.uncertain is True
        offline = await call(
            tools, owner, "shot-5", "computer", action="screenshot", device=device
        )
        assert offline.error_code == "device_offline" and not offline.uncertain
        script(connector.state, hang=False)
        await connector.start()
        again = await call(
            tools, owner, "click-3", "computer", action="click", x=5, y=5, device=device
        )
        calls["click-3-after-restart"] = view(again)
        assert again.error_code == "action_unknown" and again.uncertain is True
        assert len(events(connector.state)) == 3
    finally:
        if connector.process.returncode is None:
            stderr = await connector.stop()
            assert connector.process.returncode == 0, stderr
        write(
            "computer",
            {"calls": calls, "events": events(connector.state), "stdout": connector.lines},
        )


async def test_old_connector_without_the_resource_is_a_clear_failure(
    bridge, database, tmp_path, monkeypatch
):
    original = connector_module.Connector._device_command

    async def old(self, command):
        # Connector 0.4.4 and earlier: no computer/browser resource at all.
        if command["resource"] in {"computer", "browser"}:
            raise DriverError("invalid_resource", f"Unsupported device resource: {command}")
        return await original(self, command)

    monkeypatch.setattr(connector_module.Connector, "_device_command", old)
    async with actual_connector(bridge, tmp_path, monkeypatch):
        tools = tools_for(database, bridge)
        owner = runtime_for(bridge.env.person)
        device = bridge.device["device_id"]
        results = {}
        for tool, action in (("computer", "click"), ("browser", "snapshot")):
            result = await call(
                tools, owner, f"old-{tool}", tool, action=action, x=1, y=1, device=device
            ) if tool == "computer" else await call(
                tools, owner, f"old-{tool}", tool, action=action, device=device
            )
            results[tool] = view(result)
            assert result.error_code == "device_not_supported", result
            assert not result.uncertain
        # The in-process Connector has no screen backend: no computer facts are claimed.
        environment = await call(tools, owner, "old-env", "get_environment", device=device)
        assert "computer" not in environment.data
    write("old-connector", results)
