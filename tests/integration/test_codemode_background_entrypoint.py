"""A persisted plugin notification job remains the sole wakeup/recovery owner."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.support.codemode_cases import BINARY, requires_worker
from tests.support.parent_receipts import parent_receipts
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

pytestmark = requires_worker


@pytest.mark.parametrize("scenario", ["normal", "revoked", "cancelled", "resumed"])
async def test_background_job_code_keeps_event_owner_and_current_plugin_grant(
    database, tmp_path, scenario
):
    env = await social_env(database, tmp_path)
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id="test.background",
        name="Offline background",
        version="1.0",
        plugin_api="1",
        yuki_requires=">=3",
        entrypoint="plugin:Plugin",
        requested_permissions=(),
        manifest_hash="a" * 64,
    )
    await installations.approve("test.background")
    await installations.set_enabled("test.background", enabled=True)
    await installations.set_status("test.background", status="running")
    repository = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="20001")
    await repository.grant_target(
        plugin_id="test.background", target=target, bot_user_id="80001", created_by_user_id="10001"
    )
    published = await repository.publish(
        plugin_id="test.background",
        request=PublishNotificationRequest(
            event_key="original-event",
            event_type="fixture.created",
            external_source="offline",
            target=target,
            occurred_at=datetime.now(UTC),
            summary="Original external update",
            payload={"fixture": True},
            ask_agent=True,
            agent_intent="Acknowledge the update once.",
        ),
    )
    assert published.agent_turn_enqueued
    job = await repository.claim_turn()
    assert job is not None
    code = "await yuki_send_message({'text': 'BACKGROUND_PUBLIC'})"
    if scenario == "cancelled":
        code += "\nawait yuki_send_message({'text': 'MUST_NOT_SEND'})"
    if scenario == "resumed":
        code = (
            "for i in range(36):\n"
            "    await yuki_update_short_state({'slot': 1, 'text': str(i), "
            "'expected_revision': i})\n" + code
        )

    def respond(request):
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("outer", ToolFunction("execute_code", json.dumps({"code": code}))),
                ),
            )
        paired = parent_receipts(request, "outer")
        assert len(paired) == 1
        return "NO_REPLY"

    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="20001",
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
    )
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    chat.runtime.runner.code_mode_settings = settings
    worker = PluginBackgroundTurnWorker(
        repository=repository,
        ledger=chat._ledger,
        runtime_config=chat._runtime_config,
        chat=chat,
        turns=chat._turn_coordinator,
        conversation_scopes=chat._conversation_scopes,
        router=env.router,
    )
    downstream = tmp_path / "background-qq.jsonl"
    original_send = env.bot.call_api

    async def transport(action, **params):
        if action.startswith("send_"):
            with downstream.open("a") as output:
                output.write(json.dumps(params) + "\n")
            if scenario == "cancelled":
                await WorkRepository(database).cancel(env.context.conversation_id)
        return await original_send(action, **params)

    env.bot.call_api = transport
    if scenario == "revoked":
        await installations.set_enabled("test.background", enabled=False)
    await worker._execute(job)
    if scenario == "resumed":
        assert len(provider.requests) == 1
        assert not downstream.exists()
        async with database.immediate_session() as writer:
            await writer.execute(
                update(PluginBackgroundTurnJobModel)
                .where(PluginBackgroundTurnJobModel.id == job.id)
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        resumed = await repository.claim_turn()
        assert resumed is not None and resumed.id == job.id
        await worker._execute(resumed)
        job = resumed
    async with database.sessions() as reader:
        stored_job = await reader.get(PluginBackgroundTurnJobModel, job.id)
        rows = list(await reader.execute(select(work)))
    if scenario == "revoked":
        assert not rows and not provider.requests
    else:
        assert len(rows) == 1
        row = rows[0]._mapping
        source = json.loads(row["source_json"])
        assert source["owner"] == "plugin_background"
        assert source["plugin_id"] == job.plugin_id
        assert source["trigger_event_id"] == job.source_event_id
        assert source["actor_user_id"] == ""
        assert row["state"] == ("cancelled" if scenario == "cancelled" else "completed")
        assert row["tool_calls"] == (37 if scenario == "resumed" else 1)
    assert stored_job.status == (
        "completed"
        if scenario in {"normal", "resumed"}
        else "cancelled"
        if scenario == "revoked"
        else "failed"
    )
    before = len(provider.requests)
    await worker._execute(job)
    assert len(provider.requests) == before
    expected = int(scenario != "revoked")
    assert (len(downstream.read_text().splitlines()) if downstream.exists() else 0) == expected
