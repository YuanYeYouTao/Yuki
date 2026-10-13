"""Current deployment contracts through real Chat, SELF and due-slot Main calls."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, make_settings
from tests.integration.test_self_common_capabilities import runtime, self_actor
from tests.support.automation_runtime_helpers import FakeClock
from tests.support.model_profiles import write_fake_profiles
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.trigger import SelfInitiativeTrigger
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_journal import WorkJournal, decode_transcript
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS
from yuki_plugin_sdk.permissions import PluginPermission


def _catalog(profile):
    return ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
    )


def _profile():
    return ModelProfile(
        id="main",
        provider="gemini",
        protocol=ModelProtocol.GEMINI,
        base_url="https://wire.invalid/",
        api_key_env="SYNTHETIC_ONLY",
        model="gemini-main",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.2,
        default_max_output_tokens=4096,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
    )


async def _deployment(
    database, tmp_path, provider, *, code_enabled=False, request_limit=24, **overrides
):
    env = await social_env(database, tmp_path)
    app = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=tmp_path / "plugins",
            workspace_directory=tmp_path / "main-workspace",
            conversation_media_cache_directory=tmp_path / "media-cache",
            social_transfer_directory=tmp_path / "transfer",
            runtime_work_enabled=True,
            automation_enabled=True,
            enabled_groups_csv="20001",
            code_mode_enabled=code_enabled,
            agent_max_model_requests=request_limit,
            llm_max_output_tokens=4096,
            **overrides,
        ),
        database=database,
    )
    app.models.apply_catalog(
        _catalog(_profile()), ModelClientPool(injected_profiles={"main": provider})
    )
    app.gateway_registry.connect(env.bot, provider_id="snowluma", presence_id=env.presence)
    await app.model_invocations.writer.start()
    message = InboundMessage(
        message_id="full-deployment-inbound",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001", nickname="真实参与者"),
        text="CURRENT_CHAT_INSTRUCTION",
        bot_user_id="80001",
        group_id="20001",
        mentions_bot=True,
    )
    sender = MemorySender()
    sender.bot = env.bot
    return app, env, message, sender


def _text(body):
    return "\n".join(part.get("text", "") for item in body["contents"] for part in item["parts"])


def _fixed(body):
    return {key: value for key, value in body.items() if key != "contents"}


@pytest.mark.parametrize("protocol", ["gemini", "deepseek"])
async def test_actual_main_http_separates_host_material_before_final_user_body(
    database, tmp_path, protocol
):
    wire = []

    def transport(request):
        wire.append(json.loads(request.content))
        if protocol == "gemini":
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"role": "model", "parts": [{"text": "NO_REPLY"}]},
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 30,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 31,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "original-layout-response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "original-layout-message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "NO_REPLY"}],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 1, "total_tokens": 31},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider_type = GeminiProvider if protocol == "gemini" else DeepSeekResponsesProvider
        provider = provider_type(
            base_url="https://wire.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        app, _env, message, sender = await _deployment(database, tmp_path, provider)
        try:
            if protocol == "deepseek":
                profile = _profile().model_copy(
                    update={
                        "provider": "deepseek",
                        "protocol": ModelProtocol.RESPONSES,
                        "model": "deepseek-main",
                    }
                )
                app.models.apply_catalog(
                    _catalog(profile), ModelClientPool(injected_profiles={"main": provider})
                )
            assert (await app.processor.handle(message, sender)).reason == "chat"
            app.main_agent_contract.state.update({"slot": 1, "text": "CURRENT_MATERIAL_VALUE"})
            current = replace(
                message,
                message_id="layout-second-current",
                text="FINAL_CURRENT_USER_BODY\nEND_CURRENT_USER_BODY",
            )
            assert (await app.processor.handle(current, sender)).reason == "chat"
            assert len(wire) == 2
            if protocol == "gemini":
                assert wire[-1]["contents"][-1]["role"] == "user"
                texts = [part["text"] for part in wire[-1]["contents"][-1]["parts"]]
            else:
                assert wire[-1]["input"][-1]["role"] == "user"
                texts = [item["content"] for item in wire[-1]["input"]]
            joined = "".join(texts)
            host = texts[-2]
            assert host.startswith("\n\n当前工作：") and host.endswith("\n\n")
            assert json.loads(host.removeprefix("\n\n当前工作："))["state"] == "no_active_work"
            assert joined.count("当前工作：") == joined.count("CURRENT_MATERIAL_VALUE") == 1
            assert joined.index("当前工作：") < joined.index("CURRENT_MATERIAL_VALUE")
            assert joined.index("CURRENT_MATERIAL_VALUE") < joined.index(current.text)
            assert joined.endswith(current.text) and texts[-1].endswith(current.text)
            assert texts[-3] + host + texts[-1] in joined
            async with database.sessions() as reader:
                saved = (await reader.scalars(select(PromptProjectionModel))).one()
                assert "当前工作：" not in saved.payload_json
                assert "CURRENT_MATERIAL_VALUE" not in saved.payload_json
        finally:
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_current_deployment_three_entries_share_full_contract_and_fresh_material(
    database, tmp_path, code_enabled
):
    wire = []
    sessions = []

    def transport(request):
        wire.append(json.loads(request.content))
        sessions.append(request.headers["x-session-id"])
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": "NO_REPLY"}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 30,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 31,
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = GeminiProvider(
            base_url="https://wire.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        app, env, message, sender = await _deployment(
            database, tmp_path, provider, code_enabled=code_enabled
        )
        try:
            state = app.main_agent_contract.state
            state.update({"slot": 1, "text": "CHAT_CURRENT_STATE"})
            assert (await app.processor.handle(message, sender)).reason == "chat"
            assert len(wire) == 1 and "CHAT_CURRENT_STATE" in _text(wire[0])
            async with database.sessions() as reader:
                original_public = (await reader.scalars(select(PromptProjectionModel))).one()
                original_epoch = original_public.epoch_id
                original_items = json.loads(original_public.payload_json)

            identity = ConversationScope.group("80001", "20001")
            scope = await app.conversation_scopes.get(identity)
            actor = await self_actor(database, env)
            source = runtime(actor, env)
            config = await app.runtime_config.snapshot(group_id="20001")
            token = await app.turn_coordinator.notify_message(
                scope.runtime_scope_key, TurnOrigin.SELF_INITIATIVE
            )
            snapshot = ConversationTurnSnapshot(
                conversation_id=scope.id,
                scope_key=scope.runtime_scope_key,
                generation=scope.generation,
                trigger_event_id=None,
                coordinator_version=token.version,
                initiative_run_id=actor.initiative_run_id,
            )
            state.update({"slot": 1, "text": "SELF_CURRENT_STATE"})

            async def validate():
                assert app.turn_coordinator.is_current(token)

            await app.chat.generate_self_initiative(
                trigger=SelfInitiativeTrigger(
                    run_id=actor.initiative_run_id,
                    conversation_id=scope.id,
                    generation=scope.generation,
                    space_id=env.space,
                    presence_id=env.presence,
                    group_id="20001",
                    bot_user_id="80001",
                    instruction=actor.instruction,
                ),
                runtime=config,
                turn_token=token,
                turn_snapshot=snapshot,
                before_model_request=validate,
                source_runtime=source,
            )
            assert len(wire) == 2 and "SELF_CURRENT_STATE" in _text(wire[1])
            assert "CHAT_CURRENT_STATE" not in _text(wire[1])
            assert actor.instruction in _text(wire[1])
            async with database.sessions() as reader:
                self_public = (await reader.scalars(select(PromptProjectionModel))).one()
                assert self_public.view_key == scope.id and self_public.epoch_id == original_epoch
                assert json.loads(self_public.payload_json) == original_items

            async with database.sessions() as reader:
                current = await reader.scalar(
                    select(ChatEventModel).where(
                        ChatEventModel.platform_message_id == message.message_id
                    )
                )
            clock = FakeClock(datetime.now(UTC))
            app.time_context._clock = clock
            task = await app.automation.create_task(
                {
                    "name": "完整当前合同",
                    "goal": "SCHEDULE_CURRENT_INSTRUCTION",
                    "trigger": {"type": "after", "seconds": 1},
                    "delivery": {"target": "none"},
                },
                actor=ToolActor(
                    user_id="10001",
                    bot_user_id="80001",
                    group_id="20001",
                    origin=TurnOrigin.USER_MESSAGE,
                    instruction=message.text,
                    event_id=current.id,
                    person_id=env.person,
                    conversation_id=scope.id,
                    presence_id=env.presence,
                ),
                conversation_key=scope.runtime_scope_key,
            )
            state.update({"slot": 1, "text": ""})
            clock.advance(2)
            due = await app.automation_repository.claim_due(
                worker_id="contract-fixture",
                now=clock.now(),
                lease_seconds=30,
                clock=clock.now,
            )
            assert len(due) == 1
            await app.automation_worker._process_guarded(due[0])
            assert (await app.automation_repository.run_history(task.id))[
                0
            ].status is RunStatus.SUCCEEDED
            assert len(wire) == 3 and "SCHEDULE_CURRENT_INSTRUCTION" in _text(wire[2])
            assert "SELF_CURRENT_STATE" not in _text(wire[2]) and "短记录" not in _text(wire[2])
            async with database.sessions() as reader:
                schedule_public = (await reader.scalars(select(PromptProjectionModel))).one()
                assert schedule_public.view_key == scope.id
                assert schedule_public.epoch_id == original_epoch
                assert json.loads(schedule_public.payload_json) == original_items

            # Every actual entry uses this application's deployed manifest and system.
            assert _fixed(wire[0]) == _fixed(wire[1]) == _fixed(wire[2])
            assert sessions == [f"yuki:{scope.id}:g{scope.generation}:main"] * 3
            assert wire[0]["generationConfig"]["maxOutputTokens"] == 4096
            full = {tool.name: tool for tool in await app.main_agent_contract.definitions()}
            assert SANDBOX_TOOLS | WORKSPACE_TOOLS <= full.keys()
            assert {
                "automation_create",
                "task_control",
                "update_short_state",
                "send_message",
            } <= full.keys()
            schema = full["automation_create"].parameters
            assert "steps" not in json.dumps(schema) and "goal" in json.dumps(schema)
            async with database.sessions() as reader:
                public = "\n".join(
                    row.payload_json for row in await reader.scalars(select(PromptProjectionModel))
                )
                events = list(await reader.scalars(select(ChatEventModel)))
            assert all(
                value not in public
                for value in (
                    "CHAT_CURRENT_STATE",
                    "SELF_CURRENT_STATE",
                    actor.instruction,
                    "SCHEDULE_CURRENT_INSTRUCTION",
                )
            )
            assert len(events) == 2  # the actual seed and ordinary inbound only
            assert not sender.messages and not any(
                action.startswith("send_") for action, _ in env.bot.calls
            )
        finally:
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)


async def test_original_gemini_work_provider_rename_keeps_contract_and_receipts(database, tmp_path):
    wire, sessions, neutral = [], [], []

    def transport(request):
        wire.append(json.loads(request.content))
        sessions.append(request.headers["x-session-id"])
        if len(wire) == 1:
            parts = [
                {
                    "functionCall": {
                        "id": "original-accept",
                        "name": "task_control",
                        "args": {"action": "accept", "goal": "完成原始任务"},
                    },
                    "thoughtSignature": "original-paid-accept-signature",
                },
                {
                    "functionCall": {
                        "id": "original-send",
                        "name": "send_message",
                        "args": {"text": "ORIGINAL_SENT_RESULT"},
                    },
                    "thoughtSignature": "original-paid-send-signature",
                },
            ]
        elif len(wire) == 2:
            parts = [
                {
                    "functionCall": {"id": "original-read", "name": "time_get_current", "args": {}},
                    "thoughtSignature": "original-paid-private-read-signature",
                }
            ]
        else:
            parts = [
                {
                    "functionCall": {
                        "id": "renamed-complete",
                        "name": "task_control",
                        "args": {"action": "complete"},
                    }
                }
            ]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"role": "model", "parts": parts}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 30,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 35,
                },
            },
        )

    class ObservedGemini(GeminiProvider):
        async def complete(self, request):
            neutral.append(request)
            return await super().complete(request)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        original = ObservedGemini(
            base_url="https://wire.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
            provider_name="gemini",
        )
        app, env, message, sender = await _deployment(database, tmp_path, original, request_limit=2)
        try:
            config = await app.runtime_config.snapshot(group_id="20001")
            original_contract = await app.runtime.main_turns.recovery_contract(config)
            original_profile_revision = app.models.profile_revision(ModelTask.CHAT_AGENT)
            assert (await app.processor.handle(message, sender)).reason == "chat"
            await app.model_invocations.writer.drain()
            repository = WorkRepository(database)
            async with database.sessions() as reader:
                old_work = dict((await reader.execute(select(work))).mappings().one())
                old_journal = dict((await reader.execute(select(journal))).mappings().one())
                old_budget = dict((await reader.execute(select(budgets))).mappings().one())
                old_effects = [
                    dict(row) for row in (await reader.execute(select(effects))).mappings()
                ]
                old_invocations = [
                    (row.id, row.profile_id, row.provider, row.model, row.prompt_tokens)
                    for row in await reader.scalars(
                        select(ModelInvocationModel).order_by(ModelInvocationModel.id)
                    )
                ]
            assert old_work["state"] == "queued" and old_work["model_requests"] == 2
            assert old_budget["models"] == 2 and len(wire) == 2
            assert old_journal["contract"] == original_contract
            store = ProtocolStore(database)
            old_private = await store.hydrate(json.loads(old_journal["payload_json"]))
            transcript = decode_transcript(old_private["transcript"])
            assert transcript.request().continuation.provider == "gemini"
            assert transcript.request().continuation.profile_id == "main"
            assert "original-paid-private-read-signature" in json.dumps(old_private)
            assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1

            renamed_profile = _profile().model_copy(update={"provider": "agm"})
            assert renamed_profile.model_dump(exclude={"provider"}) == _profile().model_dump(
                exclude={"provider"}
            )
            renamed = ObservedGemini(
                base_url="https://wire.invalid/",
                api_key="synthetic",
                timeout_seconds=2,
                max_retries=0,
                client=client,
                provider_name="agm",
            )
            app.models.apply_catalog(
                _catalog(renamed_profile), ModelClientPool(injected_profiles={"main": renamed})
            )
            next_contract = await app.runtime.main_turns.recovery_contract(config)
            assert next_contract == original_contract
            assert app.models.profile_revision(ModelTask.CHAT_AGENT) == original_profile_revision
            # A connection label does not retire the original paid protocol contract.
            lease = await repository.acquire(old_work["conversation_id"], old_work["generation"])
            try:
                assert (
                    await WorkJournal(repository).load(lease, old_work["id"], next_contract)
                ).reason == "resume"
            finally:
                await repository.release(lease)
            await app.work_resumer.resume(await repository.get(old_work["id"]))
            await app.model_invocations.writer.drain()
            final = await repository.get(old_work["id"])
            assert final["id"] == old_work["id"] and final["source_key"] == old_work["source_key"]
            assert final["source_json"] == old_work["source_json"]
            assert final["state"] == "completed" and final["model_requests"] == 3
            assert len(wire) == 3 and neutral[2].request_chain_id != old_journal["chain_id"]
            assert neutral[2].continuation is None and not neutral[2].continuation_items
            assert all(item.response_item is None for item in neutral[2].messages)
            assert "original-paid-" not in json.dumps(wire[2])
            assert "ORIGINAL_SENT_RESULT" in _text(wire[2])
            assert _fixed(wire[1]) == _fixed(wire[2])
            resumed_parts = wire[2]["contents"][-1]["parts"]
            assert resumed_parts[-1]["text"] == neutral[2].messages[-1].content
            materials = [part["text"] for part in resumed_parts if part["text"].startswith("\n\n{")]
            assert {json.loads(text)["kind"] for text in materials} == {
                "work_current_material",
                "work_unobserved_tool_round",
            }
            assert all(text.endswith("\n\n") for text in materials)
            main_session = f"yuki:{old_work['conversation_id']}:g{old_work['generation']}"
            assert sessions == [
                f"{main_session}:main",
                f"{main_session}:work:{old_work['id']}",
                f"{main_session}:work:{old_work['id']}",
            ]
            assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
            async with database.sessions() as reader:
                final_budget = dict((await reader.execute(select(budgets))).mappings().one())
                retained = [
                    dict(row)
                    for row in (
                        await reader.execute(
                            select(effects).where(
                                effects.c.effect_key.in_([row["effect_key"] for row in old_effects])
                            )
                        )
                    ).mappings()
                ]
                records = [
                    (row.id, row.profile_id, row.provider, row.model, row.prompt_tokens)
                    for row in await reader.scalars(
                        select(ModelInvocationModel).order_by(ModelInvocationModel.id)
                    )
                ]
            assert final_budget["root_id"] == old_budget["root_id"] and final_budget["models"] == 3
            assert retained == old_effects
            assert records[:2] == old_invocations and [row[2] for row in records] == [
                "gemini",
                "gemini",
                "agm",
            ]
            assert await store.hydrate(json.loads(old_journal["payload_json"])) == old_private
        finally:
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)


@pytest.mark.parametrize(
    ("read_history", "source_kind", "evicted"),
    [
        (False, "self", False),
        (True, "self", False),
        (False, "person", False),
        (True, "person", False),
        (True, "person", True),
    ],
)
async def test_sdk_history_grant_uses_common_conversation_without_scope_fork(
    database, tmp_path, monkeypatch, read_history, source_kind, evicted
):
    from tests.unit.test_history_dispatch_ownership import _scene, _tool

    provider = FakeLLMProvider(lambda request: "内部结束")
    env, harness, chat, _, message = await _scene(database, tmp_path, provider)
    original_runtime = []
    run = chat._run_agent

    async def capture_runtime(conversation_key, initial_messages, tool_runtime, **kwargs):
        original_runtime.append(tool_runtime)
        return await run(conversation_key, initial_messages, tool_runtime, **kwargs)

    monkeypatch.setattr(chat, "_run_agent", capture_runtime)
    await harness.processor.handle(message, MemorySender())
    async with database.sessions() as reader:
        before = (await reader.scalars(select(PromptProjectionModel))).one()
        original = (before.epoch_id, before.revision, before.payload_json)
    if source_kind == "person":
        source = original_runtime[0]
        assert source.require_actor().event_id is not None
    else:
        actor = await self_actor(database, env)
        source = replace(runtime(actor, env), runtime_config=await chat._runtime_config.snapshot())
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    scoped = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    added = await scoped.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="new-public-source-before-sdk",
        sender_user_id="10001",
        direction="inbound",
        content="SDK先采用的真实新公共事件",
        occurred_at=datetime(2026, 10, 12, 23, 11, 12, tzinfo=UTC),
    )
    event = added.event
    host = HostPluginContext(
        plugin_id="test.common-history",
        approved_permissions=(PluginPermission.AGENT_RUN,)
        + ((PluginPermission.MESSAGE_HISTORY_READ,) if read_history else ()),
        services=PluginFacadeServices(
            approval_revision="actual-offline-grant",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    provider._responder = lambda request: _tool(
        "task_control", {"action": "complete", "result": "SDK original result"}, "complete-sdk"
    )
    if evicted:
        await chat.runtime.main_turns._projections.invalidate_view(
            message.conversation_id, reason="capacity"
        )
    with host.invocation_scope(host.plugin_id, source):
        result = await host.agent.run("当前插件实际要求")
    assert result.ok and result.data["state"] == "completed"
    actual = provider.requests[-1].messages
    public = json.loads(original[2])
    for item in public:
        assert (item["message"]["content"] in [m.content for m in actual]) is read_history
    async with database.sessions() as reader:
        rows = (await reader.scalars(select(PromptProjectionModel))).all()
        assert len(rows) == 1 and rows[0].view_key == message.conversation_id
        if not read_history:
            assert (rows[0].epoch_id, rows[0].revision, rows[0].payload_json) == original
        else:
            latest = json.loads(rows[0].payload_json)
            assert latest[: len(public)] == public
            new = [item for item in latest if event.id in item["event_ids"]]
            assert len(new) == 1 and "[07:11:12" in new[0]["message"]["content"]
            assert "SDK先采用的真实新公共事件" in "\n".join(m.content or "" for m in actual)
        adopted = json.loads(rows[0].payload_json)
        epoch = rows[0].epoch_id
    provider._responder = lambda request: "内部结束"
    await harness.processor.handle(
        replace(message, message_id="ordinary-after-sdk", text="SDK之后的普通当前输入"),
        MemorySender(),
    )
    async with database.sessions() as reader:
        final = (await reader.scalars(select(PromptProjectionModel))).one()
        assert final.epoch_id == epoch
        assert json.loads(final.payload_json)[: len(adopted)] == adopted
        assert "当前插件实际要求" not in final.payload_json
