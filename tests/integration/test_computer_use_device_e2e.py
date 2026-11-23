"""Computer Use at the Bot: the model tool → LocalDeviceService → TLS WSS → an actual
Connector process, and the frozen manifest.

Failure cases were written before the code, from docs/architecture/Computer-Use任务书-2026-11-15.md
§11: C1, C14, C15, C17 (the computer screenshot limit, hot and apart from vision_*), C7,
X1 (out of range at the Bot: failed, not uncertain), R1, R9, X2 (echo and the original
receipt), V3, D10 (own-computer actions never reach a Connector), D1–D4, D11 (capability
facts against an older Connector, including one that reconnects older), X7 (contract 38, one
frozen manifest). Only the operating system's screen is replaced
(packages/yuki-agent-connector/tests/fakes/screen_connector.py).

Artifacts: test-results/computer-use/manifest.json, capability-gating.json.
"""

import json
from pathlib import Path

from tests.conftest import build_harness, make_settings
from tests.integration.test_device_computer_e2e import (
    RETINA,
    Process,
    call,
    events,
    runtime_for,
    script,
    view,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.support.service_dependencies import tool_dependencies
from yuki_agent_connector.config import Config
from yuki_agent_connector.desk import FEATURES
from yuki_agent_connector.screen import Display, frame

from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.agent_tools import AgentToolService
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "test-results" / "computer-use"


def write(name: str, value: object) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def captures(state: Path) -> int:
    path = state / "captures.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


def tools_for(database, bridge, **settings) -> AgentToolService:
    configured = make_settings(database.url, **settings)
    tools = AgentToolService(
        settings=configured,
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        **tool_dependencies(database, configured),
    )
    tools.sandbox_client.local_devices = bridge.service.local_devices
    return tools


async def test_limits_echo_and_capability_facts(bridge, database, tmp_path):
    connector = Process(bridge, tmp_path)
    script(connector.state, hit={"role": "button", "name": "OK"})
    Config(
        bridge.websocket_url, bridge.device["device_id"], bridge.device["token"], full_access=True
    ).save(connector.data / "config.json")
    device = bridge.device["device_id"]
    # C15: the chat-image limit is tiny here; it must not touch computer screenshots.
    tools = tools_for(database, bridge, vision_max_dimension=320, vision_max_pixels=100_000)
    owner = runtime_for(bridge.env.person)
    record: dict[str, dict] = {}
    await connector.start()
    try:
        # D3: the Connector's facts list its features.
        environment = await call(tools, owner, "env", "get_environment", device=device)
        assert environment.data["computer"]["features"] == sorted(FEATURES)

        # C1/C15: 1568 px / 1.15 MP by default, whatever vision_* says.
        shot = await call(tools, owner, "shot-1", "computer", action="screenshot", device=device)
        record["shot-1"] = view(shot)
        size = frame(Display(**RETINA), 1568, 1_150_000)
        assert (shot.data["width"], shot.data["height"]) == size == (1331, 864)
        assert "scale=1000" in shot.data["coordinates"]

        # C14: a hot change applies from the next screenshot.
        await tools._runtime_config.initialize()
        changed = await tools._runtime_config.set_override(
            "computer.screenshot_max_dimension",
            800,
            scope_type="global",
            scope_id="",
            actor_user_id="operator",
            trigger_message_id="computer-use-limit",
        )
        assert changed.success, changed
        smaller = await call(tools, owner, "shot-2", "computer", action="screenshot", device=device)
        assert smaller.data["width"] == 800, view(smaller)
        await tools._runtime_config.delete_override(
            "computer.screenshot_max_dimension",
            scope_type="global",
            scope_id="",
            actor_user_id="operator",
            trigger_message_id="computer-use-limit-back",
        )

        # R1: the echo reaches the model's data; X2/R9: the same key, the same receipt.
        clicked = await call(
            tools,
            owner,
            "norm-1",
            "computer",
            action="click",
            x=500,
            y=500,
            scale=1000,
            device=device,
        )
        record["norm-1"] = view(clicked)
        assert clicked.ok and clicked.data["point"] == [666, 432] and clicked.data["scale"] == 1000
        assert clicked.data["route"] == ["global_input"]
        assert clicked.data["hit"] == {"role": "button", "name": "OK"}
        posted = len(events(connector.state))
        again = await call(
            tools,
            owner,
            "norm-1",
            "computer",
            action="click",
            x=500,
            y=500,
            scale=1000,
            device=device,
        )
        assert again.data["point"] == [666, 432] and len(events(connector.state)) == posted

        # C7/X1: outside the image: a failure with the coordinate system, never uncertain.
        outside = await call(
            tools, owner, "out-1", "computer", action="click", x=1331, y=1, device=device
        )
        record["out-1"] = view(outside)
        assert outside.error_code == "coordinate_out_of_range"
        assert not outside.uncertain and outside.mutation_committed is False
        assert outside.data["width"] == 1331 and len(events(connector.state)) == posted

        # V3/D10/D11: own-computer actions never reach the Connector.
        for action in ("paste", "wake", "snapshot"):
            refused = await call(
                tools, owner, f"own-{action}", "computer", action=action, text="x", device=device
            )
            record[f"own-{action}"] = view(refused)
            assert refused.error_code == "device_not_supported", refused
            assert refused.data["cause"] == "own_computer_only"
        assert len(events(connector.state)) == posted
    finally:
        await connector.stop()

    # D4: the same device reconnects with an older Connector (no features published).
    script(connector.state, features=[])
    await connector.start()
    try:
        before, shots = len(events(connector.state)), captures(connector.state)
        environment = await call(tools, owner, "env-old", "get_environment", device=device)
        assert environment.data["computer"]["features"] == []
        # D2: every new action or argument is refused at the Bot and never sent.
        for key, arguments, missing in (
            ("old-scale", {"action": "click", "x": 1, "y": 1, "scale": 1000}, "scale"),
            ("old-zoom", {"action": "zoom", "region": [0, 0, 10, 10]}, "zoom"),
            ("old-elements", {"action": "elements", "window": "4242"}, "elements"),
            ("old-element", {"action": "click", "element": "4242.ab12e1"}, "elements"),
            ("old-mod", {"action": "click", "x": 1, "y": 1, "text": "shift"}, "modifiers"),
            ("old-triple", {"action": "triple_click", "x": 1, "y": 1}, "triple_click"),
            ("old-middle", {"action": "middle_click", "x": 1, "y": 1}, "middle_click"),
            ("old-repeat", {"action": "key", "text": "Down", "repeat": 3}, "key_repeat"),
        ):
            refused = await call(tools, owner, key, "computer", **arguments, device=device)
            record[key] = view(refused)
            assert refused.error_code == "device_not_supported", refused
            assert refused.data["cause"] == "connector_outdated", refused
            assert missing in refused.public_message, refused.public_message
        assert len(events(connector.state)) == before and captures(connector.state) == shots
        # D1/C17: old arguments still run, at the new computer screenshot size.
        plain = await call(
            tools, owner, "old-click", "computer", action="click", x=10, y=10, device=device
        )
        record["old-click"] = view(plain)
        assert plain.ok and (plain.data["width"], plain.data["height"]) == (1331, 864)
        assert len(events(connector.state)) == before + 1
    finally:
        await connector.stop()
        write("capability-gating.json", record)


async def test_contract_38_manifest(database, tmp_path):
    """X7: one frozen manifest with the new computer contract."""
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    contract = MainAgentContract(chat, ShortState(WorkspaceStore(tmp_path / "short-state")))
    tools = {tool.name: tool for tool in await contract.definitions()}
    computer = tools["computer"].parameters
    assert {"scale", "region", "element", "repeat"} <= set(computer["properties"])
    actions = computer["properties"]["action"]["enum"]
    assert {"zoom", "elements", "triple_click", "middle_click", "paste"} <= set(actions)
    assert computer["properties"]["scale"]["type"] == "integer"
    assert "enum" not in computer["properties"]["scale"]
    assert contract.revision
    write(
        "manifest.json",
        {
            "revision": contract.revision,
            "computer": tools["computer"].parameters,
            "computer_description": tools["computer"].description,
        },
    )
