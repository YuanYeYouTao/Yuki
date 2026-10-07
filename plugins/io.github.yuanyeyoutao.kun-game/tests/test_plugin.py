"""Yuki adapter, storage, permission, and lifecycle tests."""

from __future__ import annotations

import ast
import asyncio
import math
import tomllib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import plugin
import pytest
from pydantic import BaseModel

from yuki_plugin_sdk.features import FeatureRegistry
from yuki_plugin_sdk.models import CurrentMessage, JsonValue, PermissionLevel
from yuki_plugin_sdk.registrar import CommandRegistration
from yuki_plugin_sdk.testing import FakePluginContext, FakeStorage, run_plugin_contract_tests

PLUGIN_ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 8, 2, 4, 0, tzinfo=UTC)


class RecordingRegistrar:
    def __init__(self) -> None:
        self.commands: list[CommandRegistration] = []
        self.config_schemas: list[type[BaseModel]] = []

    def register_command(self, registration: CommandRegistration) -> None:
        self.commands.append(registration)

    def register_config_schema(self, schema: type[BaseModel]) -> None:
        self.config_schemas.append(schema)


class ConflictStorage(FakeStorage):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0
        self.proposals: list[JsonValue] = []

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: JsonValue,
        value: JsonValue,
    ) -> bool:
        self.attempts += 1
        self.proposals.append(deepcopy(value))
        if self.attempts <= self.failures:
            return False
        return await super().compare_and_set(namespace, key, expected, value)


class FailingStorage(FakeStorage):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation

    async def get(self, namespace: str, key: str) -> JsonValue:
        if self.operation == "get":
            raise RuntimeError("storage unavailable")
        return await super().get(namespace, key)

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: JsonValue,
        value: JsonValue,
    ) -> bool:
        if self.operation == "compare_and_set":
            raise RuntimeError("storage unavailable")
        return await super().compare_and_set(namespace, key, expected, value)


class BrokenPeople:
    async def get_current(self) -> None:
        raise RuntimeError("profile unavailable")


def _message(
    *,
    scope_type: str = "group",
    group_id: str | None = "20001",
    user_id: str = "10001",
    text: str = "*签到",
    mentions: tuple[str, ...] = (),
) -> CurrentMessage:
    return CurrentMessage(
        message_id=f"message-{scope_type}-{group_id or user_id}",
        sender_user_id=user_id,
        scope_type=scope_type,
        group_id=group_id,
        text=text,
        received_at=NOW,
        mentioned_user_ids=mentions,
    )


async def _running(
    *,
    current: CurrentMessage | None = None,
    storage: FakeStorage | None = None,
) -> tuple[plugin.KunGamePlugin, RecordingRegistrar, FakePluginContext]:
    instance = plugin.KunGamePlugin()
    registrar = RecordingRegistrar()
    await instance.register(cast(Any, registrar))
    context = FakePluginContext(PLUGIN_ID, storage=storage or FakeStorage())
    context.messages.current = current or _message()
    context.people.current_user_id = context.messages.current.sender_user_id
    context.people.people[context.messages.current.sender_user_id] = {"display_name": "测试玩家"}
    await instance.start(context)
    return instance, registrar, context


PLUGIN_ID = "io.github.yuanyeyoutao.kun-game"


def _command(registrar: RecordingRegistrar, name: str) -> CommandRegistration:
    return next(item for item in registrar.commands if item.metadata.name == name)


async def _call(
    registrar: RecordingRegistrar,
    name: str,
    text: str,
) -> Any:
    command = _command(registrar, name)
    return await command.handler(command.argument_model.model_validate({"text": text}))


async def test_plugin_passes_host_contract_and_lifecycle() -> None:
    report = await run_plugin_contract_tests(PLUGIN_ROOT, yuki_version="3.9.0")
    assert report.passed is True
    assert report.checks == (
        "manifest",
        "permissions",
        "entrypoint",
        "register",
        "start",
        "stop",
    )


async def test_registration_separates_user_and_superuser_commands() -> None:
    instance, registrar, _ = await _running()

    assert registrar.config_schemas == [plugin.KunGameConfig]
    assert _command(registrar, "play").metadata.permission is PermissionLevel.USER
    assert _command(registrar, "admin").metadata.permission is PermissionLevel.SUPERUSER
    await instance.stop()


async def test_play_commits_versioned_state_to_scope_key() -> None:
    instance, registrar, context = await _running()

    result = await _call(registrar, "play", "签到")
    stored = await context.storage.get("state", "group:20001")

    assert result.ok is True
    assert isinstance(stored, dict)
    assert stored["schema_version"] == 1
    assert stored["scope_type"] == "group"
    assert stored["scope_id"] == "20001"
    assert stored["revision"] == 1
    await instance.stop()


async def test_same_scope_concurrency_serializes_without_lost_update() -> None:
    instance, registrar, context = await _running()

    await asyncio.gather(
        _call(registrar, "play", "买蛋 1"),
        _call(registrar, "play", "买蛋 1"),
    )
    stored = await context.storage.get("state", "group:20001")

    assert isinstance(stored, dict)
    player = stored["state"]["players"]["10001"]
    assert player["jie_cao"] == 40
    assert player["eggs"] == 2
    assert stored["revision"] == 2
    await instance.stop()


async def test_cas_retries_replay_identical_time_and_random_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ConflictStorage(failures=2)
    instance, registrar, _ = await _running(storage=storage)
    monkeypatch.setattr(plugin.secrets, "randbits", lambda _bits: 42)

    result = await _call(registrar, "play", "签到")

    assert result.ok is True
    assert storage.attempts == 3
    assert storage.proposals[0] == storage.proposals[1] == storage.proposals[2]
    await instance.stop()


async def test_repeated_cas_conflict_fails_closed() -> None:
    storage = ConflictStorage(failures=3)
    instance, registrar, context = await _running(storage=storage)

    result = await _call(registrar, "play", "签到")

    assert result.ok is False
    assert result.error_code == "kun_game.cas_conflict"
    assert await context.storage.get("state", "group:20001") is None
    await instance.stop()


@pytest.mark.parametrize(
    ("operation", "error_code"),
    [("get", "kun_game.execution_failed"), ("compare_and_set", "kun_game.storage_unavailable")],
)
async def test_storage_failures_are_reported_without_confirmed_write(
    operation: str,
    error_code: str,
) -> None:
    storage = FailingStorage(operation)
    instance, registrar, _ = await _running(storage=storage)

    result = await _call(registrar, "play", "签到")

    assert result.ok is False
    assert result.error_code == error_code
    assert await storage.list("state") == {}
    await instance.stop()


async def test_group_private_and_other_group_states_do_not_share_keys() -> None:
    instance, registrar, context = await _running()
    await _call(registrar, "play", "签到")

    context.messages.current = _message(scope_type="private", group_id=None)
    await _call(registrar, "play", "签到")
    context.messages.current = _message(group_id="20002")
    await _call(registrar, "play", "签到")

    keys = await context.storage.list("state")
    assert set(keys) == {"group:20001", "private:10001", "group:20002"}
    await instance.stop()


async def test_corrupt_state_is_reported_without_overwrite() -> None:
    instance, registrar, context = await _running()
    corrupt: JsonValue = {
        "schema_version": 1,
        "scope_type": "group",
        "scope_id": "20001",
        "revision": 8,
        "updated_at": NOW.isoformat(),
        "state": {"gid": "20001"},
    }
    await context.storage.set("state", "group:20001", corrupt)

    result = await _call(registrar, "play", "签到")

    assert result.ok is False
    assert result.error_code == "kun_game.state_corrupt"
    assert await context.storage.get("state", "group:20001") == corrupt
    await instance.stop()


async def test_display_name_failure_falls_back_without_changing_random_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugin.secrets, "randbits", lambda _bits: 99)
    first, first_registrar, first_context = await _running()
    first_result = await _call(first_registrar, "play", "签到")
    first_state = await first_context.storage.get("state", "group:20001")

    second, second_registrar, second_context = await _running()
    second_context.people = cast(Any, BrokenPeople())
    second_result = await _call(second_registrar, "play", "签到")
    second_state = await second_context.storage.get("state", "group:20001")

    assert "测试玩家" in first_result.text
    assert "10001" in second_result.text
    assert first_state == second_state
    await first.stop()
    await second.stop()


async def test_superuser_can_set_scoped_economy_config_used_by_play() -> None:
    instance, registrar, context = await _running()

    configured = await _call(registrar, "admin", "配置 蛋价 7")
    bought = await _call(registrar, "play", "买蛋 2")
    stored = await context.storage.get("state", "group:20001")

    assert configured.ok is True
    assert "egg_price=7" in configured.text
    assert bought.ok is True
    assert isinstance(stored, dict)
    assert stored["state"]["players"]["10001"]["jie_cao"] == 36
    await instance.stop()


async def test_non_finite_economy_config_is_rejected_without_write() -> None:
    instance, registrar, context = await _running()

    result = await _call(registrar, "admin", "配置 悲属性孵化率 nan")

    assert result.ok is False
    assert result.error_code == "kun_game.config_invalid"
    assert context.config.values == {}
    await instance.stop()


async def test_concurrent_auction_deal_and_delist_keep_one_kun_owner() -> None:
    instance, registrar, context = await _running()
    engine = plugin._engine
    envelope = engine.default_envelope("group", "20001", NOW)
    seller = engine.default_player("10001", game_date="2026-08-02", config=engine.GameConfig())
    buyer = engine.default_player("10002", game_date="2026-08-02", config=engine.GameConfig())
    seller["jie_cao"] = 100
    buyer["jie_cao"] = 100
    auctioned_kun = {
        "name": "托管鲲",
        "weight": 1000.0,
        "attribute": "怒",
        "alive": True,
        "killer": None,
    }
    envelope["state"]["players"] = {"10001": seller, "10002": buyer}
    envelope["state"]["auction"] = {
        "seller": "10001",
        "seller_name": "卖家",
        "kun": auctioned_kun,
        "start_price": 10,
        "current_bid": 20,
        "bidder": "10002",
        "start_time": (NOW - timedelta(minutes=10)).isoformat(),
    }
    engine.validate_envelope(envelope, scope_type="group", scope_id="20001")
    await context.storage.set("state", "group:20001", cast(JsonValue, envelope))

    await asyncio.gather(
        _call(registrar, "play", "成交"),
        _call(registrar, "admin", "强制下架"),
    )
    stored = await context.storage.get("state", "group:20001")

    assert isinstance(stored, dict)
    state = stored["state"]
    owners = sum(state["players"][user_id]["kun"] is not None for user_id in ("10001", "10002"))
    assert owners == 1
    assert state["auction"] is None
    assert sum(state["players"][user_id]["jie_cao"] for user_id in ("10001", "10002")) == 200
    assert math.isfinite(state["players"]["10001"]["jie_cao"])
    await instance.stop()


async def test_play_cannot_inject_admin_action() -> None:
    instance, registrar, context = await _running()

    result = await _call(registrar, "play", "鲲开")

    assert result.ok is True
    assert "SUPERUSER" in result.text
    assert await context.storage.get("state", "group:20001") is None
    await instance.stop()


async def test_start_requires_trusted_mentions_feature() -> None:
    instance = plugin.KunGamePlugin()
    context = FakePluginContext(PLUGIN_ID)
    context.features = FeatureRegistry({"message.normalized.v1"})

    with pytest.raises(Exception, match=r"message\.current\.mentions\.v1"):
        await instance.start(context)


def test_manifest_has_only_required_local_permissions() -> None:
    payload = tomllib.loads((PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    assert set(payload["permissions"]) == {
        "command.register",
        "message.current.read",
        "person.current.read",
        "storage.private",
        "plugin.config.read",
        "plugin.config.write",
    }
    assert payload.get("secrets", []) == []
    assert payload.get("network", {}).get("allowed_hosts", []) == []


def test_plugin_source_imports_no_host_astrbot_nonebot_or_network_client() -> None:
    tree = ast.parse((PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8"))
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert not roots & {
        "astrbot",
        "nonebot",
        "qq_ai_bot",
        "httpx",
        "requests",
        "socket",
        "urllib",
    }
