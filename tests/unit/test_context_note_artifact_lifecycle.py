"""Real SQLite notes and scoped research ownership survive view changes."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.conversation.observation_models import ContextObservationModel
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.persistence.models import ToolArtifactModel
from qq_ai_bot.runtime.work_context_note import publish_pending_note, visible_context_note
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.tool_results.access import ArtifactAccess, access_from_runtime
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.tool_results.schema import artifact_refs


@pytest.mark.parametrize("different", ["plugin", "memory", "contract"])
async def test_real_plugin_runtime_artifact_identity_is_stable_and_isolated(
    database, tmp_path, different
):
    from qq_ai_bot.domain.messages import InboundMessage, ScopeType, SenderIdentity
    from qq_ai_bot.memory.enums import MemoryScopeType
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.services.agent_tools import ToolRuntime

    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        event = await reader.scalar(select(ChatEventModel))
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        source_event_id=event.id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text=event.content,
        bot_user_id="80001",
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
        conversation_id=env.context.conversation_id,
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        memory_allowed_scopes=(MemoryScopeType.GROUP,),
        context_plugin_id="plugin.one",
        context_read_contract="stable-permission-contract",
    )
    access = access_from_runtime(runtime, generation=1)
    assert access == access_from_runtime(replace(runtime, execution_id="next-call"), generation=1)
    assert access.actor_person_id == env.person
    other_runtime = (
        replace(runtime, context_plugin_id="plugin.two")
        if different == "plugin"
        else replace(runtime, memory_allowed_scopes=())
        if different == "memory"
        else replace(runtime, context_read_contract="different-permission-contract")
    )
    store = ToolArtifactRepository(database, tmp_path / "plugin-research", retention_seconds=60)
    handle = await store.write_artifact(
        content="full original plugin research",
        provider_id="core",
        tool_name="web_search",
        media_type="text/plain",
        access=access,
    )
    assert await store.read(handle, access=access) is not None
    denied = await store.read(handle, access=access_from_runtime(other_runtime, generation=1))
    assert denied["error_code"] == "artifact_not_authorized"


async def admitted(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person, read_scope="main")
    control = WorkControl(
        repository,
        lease,
        "note-source",
        {
            "actor_person_id": access.actor_person_id,
            "origin": "user_message",
        },
        AsyncMock(),
    )
    control.bind_context_access(access)
    accepted = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": "Find the full source and verify the report",
                "output_kind": "answer",
            },
            "accept-call",
        )
    )
    assert accepted["ok"]
    return control, access


def note(ref="goal"):
    return {
        "version": 1,
        "facts": [{"text": "A clue still needs verification", "refs": [ref]}],
        "unresolved": [],
        "next_steps": [],
    }


@pytest.mark.asyncio
async def test_note_only_preserves_state_budget_goal_and_survives_checkpoint(database, tmp_path):
    control, _ = await admitted(database, tmp_path)
    before = await control.repository.get(control.current["id"])
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "update",
                "context_note": note(),
            },
            "note-call",
        )
    )
    assert result["ok"]
    saved = await control.repository.get(before["id"])
    for field in ("goal", "state", "revision", "model_requests", "tool_calls", "sent_messages"):
        assert saved[field] == before[field]
    original_note = json.loads(saved["checkpoint_json"])["context_note"]
    await control.repository.checkpoint(control.lease, before["id"], {"reason": "need a source"})
    fresh = await control.repository.get(before["id"])
    assert json.loads(fresh["checkpoint_json"])["context_note"] == original_note
    control.current = fresh
    await publish_pending_note(control)
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(ContextObservationModel)) == 1
    schema = work_control_tools()[0].parameters
    assert "context_note" in schema["properties"]
    assert "context_note" not in schema["required"]


@pytest.mark.asyncio
async def test_bad_note_does_not_overwrite_goal_or_previous_note(database, tmp_path):
    control, _ = await admitted(database, tmp_path)
    await control.execute("task_control", {"action": "update", "context_note": note()}, "valid")
    before = await control.repository.get(control.current["id"])
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "update",
                "goal": "Unrelated replacement",
                "context_note": note("effect:missing"),
            },
            "bad",
        )
    )
    assert not result["ok"] and result["error"] == "work_context_note_invalid_reference"
    assert await control.repository.get(before["id"]) == before


@pytest.mark.asyncio
async def test_note_update_preserves_real_wait_and_failure_checkpoint(database, tmp_path):
    from qq_ai_bot.runtime.work_wait import WorkWaitRepository

    control, _ = await admitted(database, tmp_path)
    await control.execute(
        "task_control", {"action": "update", "context_note": note()}, "first-note"
    )
    waiting = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "wait",
                "conditions": [{"kind": "time_due", "after_seconds": 600}],
            },
            "real-wait",
        )
    )
    assert waiting["ok"] and control.ending == "waiting_external"
    repository = WorkWaitRepository(control.repository)
    original_wait = await repository.describe(control.current["id"])
    await control.execute(
        "task_control", {"action": "update", "context_note": note()}, "second-note"
    )
    assert await repository.describe(control.current["id"]) == original_wait
    assert control.ending == "waiting_external"
    failed = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "fail",
                "reason": "The original source became unavailable",
            },
            "fail",
        )
    )
    assert failed["ok"]
    stored = await control.repository.get(control.current["id"])
    assert json.loads(stored["checkpoint_json"])["context_note"]["revision"] == 2
    # The accepted failure keeps its reason until the writer commits it.
    assert json.loads(stored["checkpoint_json"])["accepted_control"]["reason"] == (
        "The original source became unavailable"
    )


@pytest.mark.asyncio
async def test_publication_failure_retries_saved_intent_without_new_note(database, tmp_path):
    control, _ = await admitted(database, tmp_path)
    with patch(
        "qq_ai_bot.conversation.observations.ContextObservationRepository.publish_note",
        AsyncMock(side_effect=ValueError("publication refused")),
    ):
        result = json.loads(
            await control.execute(
                "task_control",
                {
                    "action": "update",
                    "context_note": note(),
                },
                "paid-note-call",
            )
        )
    assert result["ok"]
    saved = await control.repository.get(control.current["id"])
    assert json.loads(saved["checkpoint_json"])["context_note"]["revision"] == 1
    control.current = saved
    first = await publish_pending_note(control)
    assert await publish_pending_note(control) == first
    repeated = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "update",
                "context_note": note(),
            },
            "paid-note-call",
        )
    )
    assert repeated["ok"]
    assert json.loads(control.current["checkpoint_json"])["context_note"]["revision"] == 1


@pytest.mark.asyncio
async def test_small_research_stored_before_manifest_short_status_keeps_original(
    database, tmp_path
):
    _, access = await admitted(database, tmp_path)
    store = ToolArtifactRepository(database, tmp_path / "research", retention_seconds=60)
    budgeter = ToolResultBudgeter(max_characters=8000, artifacts=store, artifact_access=access)
    status = await budgeter.render(
        ToolExecutionResult(
            ok=True,
            data={"status": "running", "run_id": "r"},
            provider_id="core",
            tool_name="terminal_exec",
        )
    )
    assert status.artifact_id is None and not status.truncated
    raw = {
        "answer": "original research",
        "sources": [{"url": "https://example.org", "text": "全文雪"}],
    }
    result = await budgeter.render(
        ToolExecutionResult(ok=True, data=raw, provider_id="core", tool_name="web_search")
    )
    assert result.artifact_id and result.truncated
    assert "全文雪" not in result.text
    original = await store.read(result.artifact_id, access=access)
    assert json.loads(original["content"])["data"] == raw
    unauthorized = await store.read(
        result.artifact_id, access=replace(access, actor_person_id="other")
    )
    assert unauthorized["error_code"] == "artifact_not_authorized"
    narrower = await store.read(result.artifact_id, access=replace(access, read_scope="limited"))
    assert narrower["error_code"] == "artifact_not_authorized"


@pytest.mark.asyncio
async def test_multiple_owners_protect_gc_until_last_release(database, tmp_path):
    _, access = await admitted(database, tmp_path)
    store = ToolArtifactRepository(database, tmp_path / "research", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read_webpage",
        content="原件",
        media_type="text/plain",
        access=access,
    )
    async with database.immediate_session() as writer:
        await store.add_refs(writer, "observation", "original-observation", (handle,))
        await store.add_refs(writer, "summary", "selected-summary", (handle,))
        await writer.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert await store.cleanup() == 0
    async with database.immediate_session() as writer:
        await store.release_refs(writer, "observation", "original-observation")
    assert await store.cleanup() == 0
    assert (await store.read(handle, access=access))["content"] == "原件"
    async with database.immediate_session() as writer:
        await store.release_refs(writer, "summary", "selected-summary")
    assert await store.cleanup() == 1
    assert await store.read(handle, access=access) is None
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(artifact_refs)) == 0


@pytest.mark.asyncio
async def test_note_source_cas_and_artifact_privacy_do_not_publish_stale_data(database, tmp_path):
    control, access = await admitted(database, tmp_path)
    async with database.immediate_session() as writer:
        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=0))
    store = ToolArtifactRepository(database, tmp_path / "research", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read_webpage",
        content="sensitive original",
        media_type="text/plain",
        access=access,
    )
    from qq_ai_bot.runtime.work_context_note import validate_note

    payload, handles, revision, privacy = await validate_note(control, note(f"artifact:{handle}"))
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ExecutionTraceStateModel)
            .where(ExecutionTraceStateModel.id == 1)
            .values(privacy_generation=privacy + 1)
        )
    with pytest.raises(WorkConflict, match="work_context_note_source_changed"):
        await control.repository.patch_context_note(
            control.lease,
            control.current["id"],
            0,
            {
                "revision": 1,
                "call_key": "stale",
                "payload": payload,
                "artifact_handles": list(handles),
                "source_revision": revision,
                "privacy_generation": privacy,
            },
        )
    assert (await store.read(handle, access=access))["error_code"] == "artifact_not_authorized"
    assert "context_note" not in json.loads(
        (await control.repository.get(control.current["id"]))["checkpoint_json"]
    )


@pytest.mark.asyncio
async def test_privacy_purge_releases_saved_note_and_published_original(database, tmp_path):
    control, access = await admitted(database, tmp_path)
    store = ToolArtifactRepository(database, tmp_path / "research", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read_webpage",
        content="full original",
        media_type="text/plain",
        access=access,
    )
    result = json.loads(
        await control.execute(
            "task_control",
            {"action": "update", "context_note": note(f"artifact:{handle}")},
            "artifact-note",
        )
    )
    assert result["ok"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert await store.cleanup() == 0
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(artifact_refs)) == 2
        assert await reader.scalar(select(func.count()).select_from(ContextObservationModel)) == 1
    async with database.immediate_session() as writer:
        await WorkRepository.purge_scope(writer, access.conversation_id)
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(artifact_refs)) == 0
        assert await reader.scalar(select(func.count()).select_from(ContextObservationModel)) == 0
    assert await store.cleanup() == 1
    assert await store.read(handle, access=access) is None


@pytest.mark.asyncio
async def test_current_note_view_hides_host_metadata_and_stale_private_payload(database, tmp_path):
    control, access = await admitted(database, tmp_path)
    async with database.immediate_session() as writer:
        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=0))
    await control.execute("task_control", {"action": "update", "context_note": note()}, "saved")
    payload = await visible_context_note(control)
    assert payload == note()
    assert not {"call_key", "access", "privacy_generation", "revision"} & payload.keys()
    control.bind_context_access(replace(access, read_scope="narrowed"))
    assert await visible_context_note(control) is None
    assert await publish_pending_note(control) is None
    control.bind_context_access(access)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ExecutionTraceStateModel)
            .where(ExecutionTraceStateModel.id == 1)
            .values(privacy_generation=1)
        )
    assert await visible_context_note(control) is None
    assert await publish_pending_note(control) is None
    # Optional invalid clues do not alter the Work, its goal or receipt budgets.
    fresh = await control.repository.get(control.current["id"])
    assert fresh == control.current
