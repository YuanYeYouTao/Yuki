"""Current deployment contracts through real Chat, SELF and due-slot Main calls."""

import json
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
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.trigger import SelfInitiativeTrigger
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_journal import WorkJournal, decode_transcript
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS


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

            # Every actual entry uses this application's deployed manifest and system.
            assert _fixed(wire[0]) == _fixed(wire[1]) == _fixed(wire[2])
            assert sessions == [f"yuki:{scope.id}:g{scope.generation}:main"] * 3
            assert wire[0]["generationConfig"] == {
                "maxOutputTokens": 4096,
                "thinkingConfig": {"thinkingLevel": "low"},
            }
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
