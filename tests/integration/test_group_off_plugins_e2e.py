"""E2E: `/ai off` closes a group for chat, plugin commands and plugin sends alike.

Scene A is the Telegram runtime scene: native MockHTTP Telegram -> real SQLite, canonical
ingress, Processor, NativeGroupRecovery, CommandService and Main, with a real plugin
ExtensionRegistry, DirectCommandRouter and PluginCommandAdapter (the plugin lifecycle is
a running-id stub, as in the direct-command latency suite). Scene B is the QQ social scene
with the real HostPluginContext OneBot facade and the real notification repository.

There is one switch: ``spaces.enabled``. ``/ai off`` and ``/ai on`` in a group are the
short form of ``/ai admin group current disable|enable``; they write no other state.

Failure cases, written before the implementation:

F1  ``/ai off`` from a superuser in group A disables Space A through the existing group
    command (Space ``enabled`` false, control receipt), replies with the disable result and
    makes no model request. (Before: ``off`` reached Main as chat text; Space stayed on.)
F2  group A off: a direct plugin prefix command (``*...``) is not executed, nothing is sent,
    no model request, no chat event is recorded.
F3  group A off: ``/ai plugin run fixture play ...`` is not executed, nothing is sent.
F4  group A off: a superuser's ``!ai`` prefix call is not answered by Main and records
    nothing (there is no superuser chat exception in a closed group).
F5  group A off: an ordinary member's message and ``!ai`` call get nothing.
F6  group A off: a non-superuser's ``/ai on`` does not enable the group, calls no model and
    gets no reply (it is rejected before admission like any other inbound).
F7  group A off: a superuser's ``/ai on`` enables Space A again without chat event or model;
    the next direct plugin command in A runs again.
F8  other group B stays on throughout: its direct plugin command runs and is answered.
F9  private chat is unaffected: the same direct plugin command runs and is answered.
F10 a non-superuser's ``/ai off`` in an open group does not disable it.
F11 a plugin OneBot group send (superuser-invoked from another group) to a closed Space is
    refused before any platform call; the same send to an open Space is delivered.
F12 a plugin notification (the scheduled/active publication path) to a closed Space is
    refused at publication; the open Space is accepted.
F13 an existing OneBot receipt sent before the group closed is returned as the original
    receipt on replay, without a second platform call.

Facts go to ``test-results/group-off-plugins-e2e.json``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from tests.conftest import build_harness, make_settings
from tests.integration.test_person_execution_receipts import invocation
from tests.support.service_dependencies import social_service
from tests.support.social_identity_cases import social_env

from yuki.admin.control_resolution import ControlAccess
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.providers.telegram_ingress import TelegramGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_presence, find_space_binding
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, IdentityBindingModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.command_adapter import PluginCommandAdapter
from yuki.plugin_host.direct_command_router import DirectCommandRouter
from yuki.plugin_host.extension_registry import ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore
from yuki_plugin_sdk.models import PermissionLevel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import CommandMetadata, CommandRegistration
from yuki_plugin_sdk.results import CommandResult

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "group-off-plugins-e2e.json"
RESULTS: dict[str, Any] = {}
CODE_UNDER_TEST = (
    "src/yuki/services/policies.py",
    "src/yuki/services/processor.py",
    "src/yuki/services/group_recovery.py",
    "src/yuki/plugin_host/direct_delivery.py",
    "src/yuki/plugin_host/ownership.py",
)
SUPERUSER, MEMBER = 42, 43
GROUP_A, GROUP_B = -10042, -10043


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    table = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_group_off_plugins_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "code_sha256": {
                    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in CODE_UNDER_TEST
                },
                "scenarios_sha256": hashlib.sha256(table.encode()).hexdigest(),
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class GameArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = ""


def _update(identifier: int, *, chat: int, text: str, sender: int = SUPERUSER) -> dict:
    message = {
        "message_id": identifier,
        "date": 1792070000 + identifier,
        "from": {"id": sender, "first_name": f"P{sender}", "is_bot": False},
        "chat": {
            "id": chat,
            "type": "private" if chat > 0 else "supergroup",
            "title": f"G{chat}",
        },
        "text": text,
    }
    return {"update_id": identifier, "message": message}


@pytest.fixture
async def scene(database, tmp_path):
    wire: list[tuple[str, dict]] = []

    def http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        wire.append((method, params))
        if method == "getMe":
            result: Any = {"id": 80001, "is_bot": True, "username": "yuki_test"}
        elif method == "getChatMember":
            result = {"status": "administrator", "user": {"id": params["user_id"]}}
        elif method == "sendChatAction":
            result = True
        elif method == "sendMessage":
            result = {"message_id": 1000 + len(wire), "chat": {"id": params["chat_id"]}}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})

    def respond(request):
        if request.messages[-1].role == "tool":
            return "done"
        return ChatResponse(
            "", 0, tool_calls=(ToolCall("send", ToolFunction("send", '{"text":"main reply"}')),)
        )

    model = FakeLLMProvider(respond)
    settings = make_settings(
        database.url, per_user_requests_per_minute=500, per_group_requests_per_minute=500
    )
    played: list[str] = []

    async def play(arguments: GameArguments) -> CommandResult:
        played.append(arguments.text)
        return CommandResult(text=f"played {arguments.text}")

    registry = ExtensionRegistry()
    registry.registrar("fixture", [PluginPermission.COMMAND_REGISTER]).register_command(
        CommandRegistration(
            metadata=CommandMetadata(
                name="play", description="fixture game", permission=PermissionLevel.USER
            ),
            argument_model=GameArguments,
            handler=play,
        )
    )
    manager = SimpleNamespace(running_plugin_ids=("fixture",))
    router = DirectCommandRouter(bindings={"*": "fixture:play"}, registry=registry, manager=manager)
    harness = build_harness(database, settings, model, direct_plugin_commands=router)
    harness.processor._commands._plugin_commands = PluginCommandAdapter(
        manager=manager, registry=registry, control=ControlAccess(database), direct_commands=router
    )
    gateways = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    presence_router = PresenceRouter(database, gateways)
    harness.processor._canonical_ingress = CanonicalIngressResolver(
        database, gateways, presence_router
    )
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, presence_router)
    harness.processor._group_recovery = NativeGroupRecovery(
        harness.settings, database, gateways, presence_router
    )
    harness.processor._chat._tools.social_service = social_service(
        database, presence_router, harness.scoped_events
    )
    harness.processor._chat.runtime.runner.main_contract = MainAgentContract(
        harness.processor._chat, ShortState(WorkspaceStore(tmp_path / "workspace"))
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        client = TelegramClient("offline-test-token", client=transport)
        await client.get_me()
        async with database.immediate_session() as session:
            presence = await create_presence(
                session,
                external_account_id=client.self_id,
                platform="telegram",
                now=datetime.now(UTC),
            )
        gateways.connect(client, provider_id="telegram", presence_id=presence.id)
        gateway = TelegramGateway(
            database, gateways, presence_router, harness.processor, harness.scoped_events, client
        )
        gateway.presence_id = presence.id
        env = SimpleNamespace(
            gateway=gateway, model=model, wire=wire, played=played, database=database, ids=[0]
        )

        async def say(text: str, *, chat: int, sender: int = SUPERUSER) -> dict:
            env.ids[0] += 1
            sends_before = len(_sends(env))
            requests_before = len(model.requests)
            played_before = len(played)
            events_before = await _event_count(database)
            await gateway.receive(_update(env.ids[0], chat=chat, text=text, sender=sender))
            if gateway._turns:
                import asyncio

                await asyncio.wait_for(asyncio.gather(*tuple(gateway._turns)), 10)
            return {
                "sent": [p["text"] for p in _sends(env)[sends_before:]],
                "model_requests": len(model.requests) - requests_before,
                "plugin_calls": played[played_before:],
                "chat_events": await _event_count(database) - events_before,
            }

        env.say = say
        await say("hello", chat=SUPERUSER)
        await say("hello", chat=MEMBER, sender=MEMBER)
        async with database.immediate_session() as session:
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.platform == "telegram",
                    IdentityBindingModel.external_account_id == str(SUPERUSER),
                )
            )
            (await session.get(CanonicalPersonModel, binding.person_id)).is_superuser = True
        await say("!ai establish", chat=GROUP_A)
        await say("!ai establish", chat=GROUP_B)
        yield env
        await gateway.close()


def _sends(env) -> list[dict]:
    return [params for method, params in env.wire if method == "sendMessage"]


async def _event_count(database) -> int:
    async with database.read_session() as session:
        return int(await session.scalar(select(func.count(ChatEventModel.id))))


async def _space(database, chat: int) -> CanonicalSpaceModel:
    async with database.read_session() as session:
        binding = await find_space_binding(session, str(chat), platform="telegram")
        return await session.get(CanonicalSpaceModel, binding.space_id)


async def test_ai_off_closes_group_for_chat_and_plugins(scene):
    env, database = scene, scene.database
    facts: dict[str, Any] = {}

    off = await env.say("/ai off", chat=GROUP_A)
    space_a = await _space(database, GROUP_A)
    async with database.read_session() as session:
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.result_resource_id == space_a.id
            )
        )
    facts["F1_ai_off"] = {
        **off,
        "space_enabled": space_a.enabled,
        "receipt_status": receipt.status if receipt is not None else None,
    }

    facts["F2_direct_plugin_command"] = await env.say("* 养成", chat=GROUP_A)
    facts["F3_ai_plugin_run"] = await env.say("/ai plugin run fixture play 养成", chat=GROUP_A)
    facts["F4_superuser_prefix_call"] = await env.say("!ai 在吗", chat=GROUP_A)
    facts["F5_member_message"] = await env.say("普通消息", chat=GROUP_A, sender=MEMBER)
    facts["F5_member_prefix_call"] = await env.say("!ai 在吗", chat=GROUP_A, sender=MEMBER)
    member_on = await env.say("/ai on", chat=GROUP_A, sender=MEMBER)
    facts["F6_member_ai_on"] = {
        **member_on,
        "space_enabled": (await _space(database, GROUP_A)).enabled,
    }
    facts["F8_other_group_plugin_command"] = await env.say("* 养成", chat=GROUP_B)
    facts["F9_private_plugin_command"] = await env.say("* 养成", chat=SUPERUSER)

    on = await env.say("/ai on", chat=GROUP_A)
    facts["F7_superuser_ai_on"] = {**on, "space_enabled": (await _space(database, GROUP_A)).enabled}
    facts["F7_plugin_command_after_on"] = await env.say("* 再来", chat=GROUP_A)

    member_off = await env.say("/ai off", chat=GROUP_A, sender=MEMBER)
    facts["F10_member_ai_off"] = {
        **member_off,
        "space_enabled": (await _space(database, GROUP_A)).enabled,
    }
    facts["group_b_enabled_at_end"] = (await _space(database, GROUP_B)).enabled
    RESULTS["scene_a_telegram"] = facts

    f1 = facts["F1_ai_off"]
    assert f1["space_enabled"] is False and f1["receipt_status"] == "succeeded", f1
    assert f1["model_requests"] == 0 and len(f1["sent"]) == 1 and "停用" in f1["sent"][0], f1
    for key in (
        "F2_direct_plugin_command",
        "F3_ai_plugin_run",
        "F4_superuser_prefix_call",
        "F5_member_message",
        "F5_member_prefix_call",
    ):
        closed = facts[key]
        assert closed == {
            "sent": [],
            "model_requests": 0,
            "plugin_calls": [],
            "chat_events": 0,
        }, (key, closed)
    f6 = facts["F6_member_ai_on"]
    assert f6["space_enabled"] is False and f6["model_requests"] == 0, f6
    assert f6["plugin_calls"] == [] and f6["chat_events"] == 0 and f6["sent"] == [], f6
    f8, f9 = facts["F8_other_group_plugin_command"], facts["F9_private_plugin_command"]
    assert f8["plugin_calls"] == ["养成"] and f8["sent"] == ["played 养成"], f8
    assert f9["plugin_calls"] == ["养成"] and f9["sent"] == ["played 养成"], f9
    f7 = facts["F7_superuser_ai_on"]
    assert f7["space_enabled"] is True and f7["model_requests"] == 0, f7
    assert f7["chat_events"] == 0 and f7["sent"][0].startswith("已启用当前群"), f7
    again = facts["F7_plugin_command_after_on"]
    assert again["plugin_calls"] == ["再来"] and again["sent"] == ["played 再来"], again
    f10 = facts["F10_member_ai_off"]
    assert f10["space_enabled"] is True and f10["model_requests"] == 0, f10
    assert facts["group_b_enabled_at_end"] is True


async def test_plugin_onebot_group_send_respects_closed_group(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, env.person)).is_superuser = True
        closed = await find_space_binding(writer, "2002")
        (await writer.get(CanonicalSpaceModel, closed.space_id)).enabled = False
        source = await writer.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    calls: list[tuple[str, dict]] = []

    class Gateway:
        async def call_api(self, action, params):
            calls.append((action, params))
            return {"message_id": f"native-{len(calls)}"}

    host = HostPluginContext(
        plugin_id="test.group-off-onebot",
        approved_permissions=(PluginPermission.ONEBOT_SEND,),
        services=PluginFacadeServices(ledger=EventLedgerRepository(database), social=env.service),
    )
    original = await invocation(
        database, host.plugin_id, source, platform="qq", space_id=env.space, gateway=Gateway()
    )
    facts: dict[str, Any] = {}
    try:
        with host.bind(original):
            delivered = await host.onebot.send_group("2001", "发往开启群")
            facts["F11_open"] = {"ok": delivered.ok, "platform_calls": len(calls)}
            try:
                refused = await host.onebot.send_group("2002", "发往已关闭群")
                facts["F11_closed"] = {"ok": refused.ok, "error": refused.error_code}
            except Exception as exc:  # the facade may surface the refusal as an error
                facts["F11_closed"] = {"ok": False, "error": type(exc).__name__ + ":" + str(exc)}
            facts["F11_closed"]["platform_calls"] = len(calls) - 1
        async with database.immediate_session() as writer:
            open_space = await find_space_binding(writer, "2001")
            (await writer.get(CanonicalSpaceModel, open_space.space_id)).enabled = False
        with host.bind(original):
            replay = await host.onebot.send_group("2001", "发往开启群")
        facts["F13_replay_after_close"] = {
            "ok": replay.ok,
            "same_operation": replay.data["operation_id"] == delivered.data["operation_id"],
            "platform_calls": len(calls),
        }
    finally:
        await host.close_host_resources()
    RESULTS["scene_b_onebot"] = facts
    assert facts["F11_closed"]["ok"] is False and facts["F11_closed"]["platform_calls"] == 0, facts
    assert facts["F11_open"] == {"ok": True, "platform_calls": 1}, facts
    assert facts["F13_replay_after_close"] == {
        "ok": True,
        "same_operation": True,
        "platform_calls": 1,
    }, facts


async def test_plugin_notification_to_closed_group_is_refused(database):
    from yuki.conversation.hydrate import ensure_canonical_conversation
    from yuki.identity.canonical_repository import find_presence
    from yuki.plugin_host.db_models import PluginInstallationModel
    from yuki.plugin_host.notification_repository import PluginNotificationRepository
    from yuki_plugin_sdk.api import PLUGIN_API_VERSION
    from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

    now = datetime.now(UTC)
    plugin_id = "test.group-off-notification"
    targets = {}
    async with database.immediate_session() as session:
        presence = await find_presence(session, "8000")
        for chat in ("2001", "2002"):
            space = await find_space_binding(session, chat)
            conversation = await ensure_canonical_conversation(
                session,
                kind="space",
                primary_scope_key=f"bot:8000:group:{chat}",
                space_id=space.space_id,
                space_binding_id=space.id,
            )
            targets[chat] = NotificationTarget(
                target_type="group",
                target_id=space.space_id,
                conversation_id=conversation.conversation_id,
            )
        session.add(
            PluginInstallationModel(
                plugin_id=plugin_id,
                name="Group off fixture",
                version="1.0.0",
                plugin_api=PLUGIN_API_VERSION,
                yuki_requires=">=3.8",
                manifest_hash="fixture",
                entrypoint="fixture:plugin",
                status="running",
                enabled=True,
                approved_permissions_json="[]",
                requested_permissions_json="[]",
                failure_count=0,
                discovered_at=now,
                approved_at=now,
                started_at=now,
                updated_at=now,
            )
        )
        creator = (await session.scalar(select(CanonicalPersonModel.id).limit(1))) or ""
    repository = PluginNotificationRepository(database)
    for target in targets.values():
        await repository.grant_target(
            plugin_id=plugin_id,
            target=target,
            bot_user_id="8000",
            created_by_user_id=creator,
            presence_id=presence.id,
        )
    async with database.immediate_session() as session:
        (await session.get(CanonicalSpaceModel, targets["2002"].target_id)).enabled = False

    def request(chat: str) -> PublishNotificationRequest:
        return PublishNotificationRequest(
            event_key=f"tick-{chat}",
            event_type="fixture.tick",
            external_source="fixture",
            target=targets[chat],
            occurred_at=now,
            summary="scheduled tick",
            payload={},
            text="定时通知",
        )

    facts: dict[str, Any] = {}
    try:
        closed = await repository.publish(plugin_id=plugin_id, request=request("2002"))
        facts["F12_closed"] = {"accepted": True, "status": getattr(closed, "status", None)}
    except Exception as exc:
        facts["F12_closed"] = {"accepted": False, "error": type(exc).__name__ + ":" + str(exc)}
    opened = await repository.publish(plugin_id=plugin_id, request=request("2001"))
    facts["F12_open"] = {"accepted": opened is not None}
    RESULTS["scene_b_notification"] = facts
    assert facts["F12_closed"]["accepted"] is False, facts
    assert facts["F12_open"]["accepted"] is True, facts
