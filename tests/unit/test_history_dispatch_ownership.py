"""Only an admitted fresh composition may publish its ordinary input snapshot."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.runtime_execution import make_work_resumer
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError
from qq_ai_bot.services.turn_transcript import DispatchOrigin, TranscriptRequest, validating_request
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


async def _scene(database, tmp_path, provider, *, request_limit=24, code_enabled=False):
    env = await social_env(database, tmp_path)
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            enabled_groups_csv="20001",
            agent_max_model_requests=request_limit,
            code_mode_enabled=code_enabled,
        ),
        provider,
    )
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "state"))
    state.update({"slot": 1, "text": "original-snapshot", "expected_revision": 0})
    chat.runtime.runner.main_contract = MainAgentContract(chat, state, code_enabled=code_enabled)
    chat.runtime.runner.code_mode_settings = harness.settings
    chat._tools.short_state = state
    message = InboundMessage(
        message_id="history-ownership",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001", nickname="original-nickname"),
        text="请处理这个工作",
        bot_user_id="80001",
        group_id="20001",
        mentions_bot=True,
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        presence_id=env.presence,
        space_id=env.space,
    )
    return env, harness, chat, state, message


def _tool(name, arguments, call_id):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


async def test_real_work_restore_keeps_private_tail_out_of_ordinary_projection(database, tmp_path):
    provider = FakeLLMProvider()

    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _tool(
                "task_control",
                {"action": "accept", "goal": "保存工作结果", "output_kind": "state_change"},
                "accept",
            )
        if number == 2:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "private-work-result", "expected_revision": 1},
                "update",
            )
        if number == 3:
            return _tool("task_control", {"action": "complete"}, "complete")
        raise AssertionError("unexpected model replay")

    provider._responder = respond
    env, harness, chat, state, message = await _scene(database, tmp_path, provider, request_limit=2)
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat" and len(provider.requests) == 2
    repository = WorkRepository(database)
    async with database.sessions() as session:
        saved = (await session.scalars(select(PromptProjectionModel))).one()
        original = (saved.epoch_id, saved.revision, saved.payload_json)
        assert saved.invalidated_reason is None
        assert "original-snapshot" in saved.payload_json
        assert "private-work-result" not in saved.payload_json
        assert "no_active_work" not in saved.payload_json
    from qq_ai_bot.runtime.work_schema_v1 import work

    async with database.sessions() as session:
        identity = await session.scalar(select(work.c.id))
    item = await repository.get(identity)
    assert item["state"] == "queued" and item["model_requests"] == 2
    assert state.snapshot()[0]["text"] == "private-work-result"
    state.update({"slot": 1, "text": "new-unsubmitted-state", "expected_revision": 2})

    # A safe paired root resumes current history and task material, retaining
    # original execution facts while retiring the previous private tool tail.
    await database.close()
    chat.runtime.main_turns = MainAgentTurnService(
        chat._prompt_composer, chat.runtime.runner, database
    )
    resumer = make_work_resumer(
        repository,
        ledger=harness.ledger,
        scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        router=env.router,
        config=chat._runtime_config,
        generate_self=chat.generate_self_initiative,
        generate_wakeup=chat.generate_main_agent_wakeup,
        validate_snapshot=chat.validate_turn_snapshot,
        run_effect=chat.run_effect,
        bindings=chat.runtime.bindings,
    )
    await resumer.resume(item)
    assert resumer.last_error is None
    assert len(provider.requests) == 3
    serialized = json.dumps([m.content for m in provider.requests[2].messages], ensure_ascii=False)
    assert "保存工作结果" in serialized and "work_current_material" in serialized
    assert "new-unsubmitted-state" in serialized
    assert not any(message.tool_calls for message in provider.requests[2].messages)
    async with database.sessions() as session:
        saved = (await session.scalars(select(PromptProjectionModel))).one()
        assert (saved.epoch_id, saved.payload_json) == (original[0], original[2])
        assert "work_resume" not in saved.payload_json
        assert "work_current_material" not in saved.payload_json
        assert "private-work-result" not in saved.payload_json
        assert saved.invalidated_reason is None
    completed = await repository.get(identity)
    assert completed["state"] == "completed" and completed["model_requests"] == 3


async def _pending_composition(database, tmp_path):
    provider = FakeLLMProvider()
    provider._responder = lambda request: (
        _tool("send_message", {"text": "确认。"}, "send")
        if len(provider.requests) == 1
        else "已发送。"
    )
    _, harness, chat, _, message = await _scene(database, tmp_path, provider)
    prepared = []
    compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        prepared.append(kwargs)
        return await compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat"
    composition = await compose(**prepared[0])
    return chat, composition


async def test_recovery_origin_does_not_invalidate_a_new_composition(database, tmp_path):
    _, composition = await _pending_composition(database, tmp_path)
    async with database.sessions() as session:
        saved = (await session.scalars(select(PromptProjectionModel))).one()
        original = (saved.epoch_id, saved.revision, saved.payload_json)
    sequence = TranscriptRequest(
        messages=(ChatMessage(role="user", content="private restored task"),),
        continuation=None,
        items=(),
        origin=DispatchOrigin.WORK_RECOVERY,
    )
    with validating_request(sequence):
        await composition.commit_projection()
    async with database.sessions() as session:
        saved = (await session.scalars(select(PromptProjectionModel))).one()
        assert (saved.epoch_id, saved.revision, saved.payload_json) == original
        assert saved.invalidated_reason is None


async def test_recovery_origin_still_checks_source_authority(database, tmp_path, monkeypatch):
    chat, composition = await _pending_composition(database, tmp_path)
    validate_source = AsyncMock(return_value=False)
    commit = AsyncMock()
    monkeypatch.setattr(chat._ledger, "read_version_matches", validate_source)
    validator = chat._context_validator(composition.read_version, commit_projection=commit)
    sequence = TranscriptRequest((), None, (), origin=DispatchOrigin.WORK_RECOVERY)
    with validating_request(sequence), pytest.raises(HistorySourceChangedError):
        await validator()
    validate_source.assert_awaited_once()
    commit.assert_not_awaited()


async def test_declared_public_suffix_must_exist_in_actual_dispatch(database, tmp_path):
    _, composition = await _pending_composition(database, tmp_path)
    sequence = TranscriptRequest(
        messages=composition.messages,
        continuation=None,
        items=(),
        public_initial_suffix=(ChatMessage(role="user", content="not actually dispatched"),),
    )
    with validating_request(sequence):
        await composition.commit_projection()
    async with database.sessions() as session:
        saved = (await session.scalars(select(PromptProjectionModel))).one()
        assert saved.invalidated_reason == "protocol_changed"
        assert "not actually dispatched" not in saved.payload_json


@pytest.mark.parametrize(
    "boundary", ["selected_scope", "plugin_permission", "deleted", "profile", "contract"]
)
async def test_same_actor_new_turn_honors_snapshot_boundaries(
    database, tmp_path, monkeypatch, boundary
):
    provider = FakeLLMProvider()
    provider._responder = lambda request: (
        _tool("send_message", {"text": "确认。"}, f"send-{len(provider.requests)}")
        if len(provider.requests) % 2
        else "已发送。"
    )
    _, harness, chat, state, message = await _scene(database, tmp_path, provider)
    profile_revision = {"value": "original-profile"}
    monkeypatch.setattr(
        chat.runtime.runner._models,
        "profile_revision",
        lambda task: profile_revision["value"],
        raising=False,
    )
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat" and len(provider.requests) == 2
    async with database.sessions() as reader:
        saved = (await reader.scalars(select(PromptProjectionModel))).one()
        original_epoch, original_key = saved.epoch_id, saved.view_key
        assert "original-snapshot" in saved.payload_json
        original_event = await reader.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.platform_message_id == message.message_id
            )
        )
    state.update({"slot": 1, "text": "current-safe-snapshot", "expected_revision": 1})
    if boundary == "deleted":
        async with database.sessions() as writer, writer.begin():
            await writer.delete(await writer.get(ChatEventModel, original_event))
        async with database.sessions() as reader:
            retired = await reader.get(PromptProjectionModel, original_key)
            assert retired.invalidated_reason == "deleted_event"
            assert retired.payload_json == "[]"
    elif boundary == "profile":
        profile_revision["value"] = "changed-profile"
    elif boundary == "contract":
        contract = chat.runtime.runner.main_contract
        declared = await contract.definitions()
        contract._tools = tuple(
            replace(tool, description=tool.description + " Updated contract.") for tool in declared
        )
        contract.revision = "changed-manifest-revision"
    else:
        compose = chat.runtime.main_turns.compose

        async def restricted_compose(**kwargs):
            if boundary == "plugin_permission":
                # This flag is the existing caller-owned context permission,
                # independent of the fixed declaration/execution capability set.
                kwargs["include_plugin_context"] = False
            else:
                # Model the assembler's selected set after narrowing the read
                # policy/window. Do not invent a wider source or actor grant.
                context = kwargs["context"]
                kwargs["context"] = replace(
                    context,
                    history_messages=(),
                    history_fragments=(),
                    history_event_fragments=(),
                    visible_event_ids=frozenset({context.current_event_id}),
                )
                kwargs["read_scope"] = "restricted-current-event"
            return await compose(**kwargs)

        monkeypatch.setattr(chat.runtime.main_turns, "compose", restricted_compose)
    await database.close()
    next_message = replace(message, message_id="history-boundary-next", text="继续说明当前状态")
    result = await harness.processor.handle(next_message, MemorySender())
    assert result.reason == "chat" and len(provider.requests) == 4
    current_input = json.dumps(
        [m.content for m in provider.requests[2].messages], ensure_ascii=False
    )
    assert "current-safe-snapshot" in current_input
    if boundary in {"profile", "contract"}:
        assert "original-snapshot" in current_input
    else:
        assert "original-snapshot" not in current_input
    async with database.sessions() as reader:
        rows = (await reader.scalars(select(PromptProjectionModel))).all()
        active = [row for row in rows if row.invalidated_reason is None]
        current = next(row for row in active if "current-safe-snapshot" in row.payload_json)
        assert current.epoch_id != original_epoch and current.revision == 1
        if boundary in {"profile", "contract"}:
            assert "original-snapshot" in current.payload_json
        else:
            assert "original-snapshot" not in current.payload_json
        if boundary in {"plugin_permission", "selected_scope"}:
            assert current.view_key != original_key
        else:
            assert current.view_key == original_key
            assert (
                current.rebuild_reason
                == {
                    "selected_scope": "capacity",
                    "deleted": "deleted_event",
                    "profile": "contract_changed",
                    "contract": "contract_changed",
                }[boundary]
            )
    if boundary == "contract":
        assert provider.requests[2].tools != provider.requests[0].tools
    elif boundary == "profile":
        assert provider.requests[2].tools == provider.requests[0].tools
