"""A native tool checkpoint must not discard the approved ordinary input prefix."""

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_enabled", [False, True])
@pytest.mark.parametrize("http_retry", [False, True])
async def test_native_checkpoint_preserves_public_prefix_across_ordinary_turns(
    database, tmp_path, monkeypatch, runtime_enabled, http_retry
):
    wire = []
    attempts = []
    runtime_observation = {"text": "original-runtime-state"}
    participation = {"thread": "original-participation", "engage": "stay"}
    original_runtime_state = WorkControl.runtime_state

    async def runtime_state(control):
        return {
            **await original_runtime_state(control),
            "test_scoped_observation": runtime_observation["text"],
        }

    monkeypatch.setattr(WorkControl, "runtime_state", runtime_state)

    def transport(request):
        payload = json.loads(request.content)
        attempts.append(payload)
        if http_retry and len(attempts) == 1:
            return httpx.Response(
                503, json={"error": {"message": "transient pre-response failure"}}
            )
        wire.append(payload)
        index = len(wire)
        if index == 1:
            parts = [
                {
                    "functionCall": {"name": "get_my_capabilities", "args": {}, "id": "read-1"},
                    "thoughtSignature": "original-signature",
                }
            ]
        elif index in {2, 4, 6}:
            parts = [
                {
                    "functionCall": {
                        "name": "send_message",
                        "args": {"text": "我是用 Python 写的。"},
                        "id": f"send-{index}",
                    },
                    "thoughtSignature": f"send-signature-{index}",
                }
            ]
        else:
            parts = [{"text": "已发送。"}]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": parts},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://gemini.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = GeminiProvider(
            base_url="https://gemini.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=int(http_retry),
            client=client,
        )
        harness = build_harness(
            database,
            make_settings(database.url, runtime_work_enabled=runtime_enabled),
            provider,
        )
        chat = harness.processor._chat

        async def participation_context(event_id):
            assert event_id > 0
            return dict(participation)

        chat.participation_context = participation_context
        profile = ModelProfile(
            id="gemini",
            provider="gemini",
            protocol=ModelProtocol.GEMINI,
            base_url="https://gemini.invalid",
            api_key_env="UNUSED",
            model="gemini-3.8-flash",
            timeout_seconds=1,
            max_retries=int(http_retry),
            default_temperature=0.5,
            default_max_output_tokens=8192,
            capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
        )
        models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={"gemini": profile},
                    routes={task: ModelRoute(task=task, profile_id="gemini") for task in ModelTask},
                )
            ),
            pool=ModelClientPool(injected_profiles={"gemini": provider}),
        )
        chat.runtime.runner._models = models
        chat._models = models
        state = ShortState(WorkspaceStore(tmp_path / "state"))
        state.update({"slot": 1, "text": "original-private-state", "expected_revision": 0})
        chat.runtime.runner.main_contract = MainAgentContract(chat, state)
        chat._tools.short_state = state
        async with database.sessions() as session, session.begin():
            person = await ensure_person(session, "1001")
            other_person = await ensure_person(session, "1002")
            presence = await ensure_presence(session, "9999")
            space = await ensure_space(session, "2002")
            conversation = await ensure_canonical_conversation(
                session, kind="space", primary_scope_key="bot:9999:group:2002", space_id=space
            )
        message = InboundMessage(
            message_id="gemini-prefix-1",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("1001", nickname="original-nickname"),
            text="Yuki 是用 Python 写的吗？",
            bot_user_id="9999",
            group_id="2002",
            mentions_bot=True,
            conversation_id=conversation.conversation_id,
            legacy_conversation_key="bot:9999:group:2002",
            person_id=person,
            presence_id=presence,
            space_id=space,
        )
        sender = MemorySender()
        result = await harness.processor.handle(message, sender)
        assert result.reason == "chat" and len(wire) == 3
        assert ("original-runtime-state" in json.dumps(wire[0])) is runtime_enabled
        if http_retry:
            assert attempts[0] == attempts[1]
        assert wire[1]["contents"][-2]["parts"][0]["thoughtSignature"] == "original-signature"
        receipt = wire[1]["contents"][-1]["parts"][0]["functionResponse"]
        assert receipt["id"] == "read-1" and receipt["name"] == "get_my_capabilities"
        async with database.sessions() as session:
            saved = (await session.scalars(select(PromptProjectionModel))).one()
            epoch, frozen = saved.epoch_id, saved.payload_json
            assert saved.invalidated_reason is None
            assert saved.revision == 1
            assert "original-signature" not in frozen and "functionResponse" not in frozen
            assert "original-runtime-state" not in frozen
            assert "original-participation" in frozen

        # Reopen the service and change dynamic data before the next user turn.
        await database.close()
        chat.runtime.main_turns = MainAgentTurnService(
            chat._prompt_composer, chat.runtime.runner, database
        )
        state.update({"slot": 1, "text": "current-state", "expected_revision": 1})
        runtime_observation["text"] = "current-runtime-state"
        participation.update(thread="current-participation", engage="quiet")
        result = await harness.processor.handle(
            replace(
                message,
                message_id="gemini-prefix-2",
                text="再介绍一下你自己",
                sender=SenderIdentity("1001", nickname="current-nickname"),
            ),
            sender,
        )
        assert result.reason == "chat" and len(wire) == 5
        first_parts = wire[0]["contents"][0]["parts"]
        if runtime_enabled:
            # Runtime/Work status is an execution-local tail, never part of the
            # frozen public chat. Its same-activation position stays untouched.
            assert "original-runtime-state" in json.dumps(first_parts[-1])
            first_parts = first_parts[:-1]
        assert wire[3]["contents"][0]["parts"][: len(first_parts)] == first_parts
        assert len(wire[3]["contents"][0]["parts"]) > len(first_parts)
        assert wire[3]["systemInstruction"] == wire[0]["systemInstruction"]
        assert wire[3]["tools"] == wire[0]["tools"]
        assert wire[3]["toolConfig"] == wire[0]["toolConfig"]
        assert wire[3]["generationConfig"] == wire[0]["generationConfig"]
        serialized = json.dumps(wire[3], ensure_ascii=False)
        assert "original-private-state" in serialized and "current-state" in serialized
        assert "original-participation" in serialized and "current-participation" in serialized
        assert "original-signature" not in serialized
        if runtime_enabled:
            assert "original-runtime-state" not in serialized
            assert "current-runtime-state" in serialized
        async with database.sessions() as session:
            saved = (await session.scalars(select(PromptProjectionModel))).one()
            assert saved.epoch_id == epoch and saved.rebuild_reason == "bootstrap"
            assert json.loads(saved.payload_json)[: len(json.loads(frozen))] == json.loads(frozen)

        # A different actor has a separate read view and cannot inherit that envelope.
        result = await harness.processor.handle(
            replace(
                message,
                message_id="gemini-prefix-3",
                text="也向我介绍一下",
                sender=SenderIdentity("1002", nickname="another-actor"),
                person_id=other_person,
            ),
            sender,
        )
        assert result.reason == "chat" and len(wire) == 7
        assert "original-private-state" not in json.dumps(wire[5], ensure_ascii=False)
        assert "original-runtime-state" not in json.dumps(wire[5], ensure_ascii=False)
