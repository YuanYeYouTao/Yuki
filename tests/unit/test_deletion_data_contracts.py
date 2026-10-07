"""Real database races and bounded cleanup after deleting duplicate owners."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_native_tool_media import _result

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.models import ToolArtifactModel
from qq_ai_bot.tool_results.access import ArtifactAccess
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize("operation", ["text", "get", "inspect", "image"])
async def test_one_post_io_artifact_fence_rejects_concurrent_generation(
    database, tmp_path, monkeypatch, operation
):
    import qq_ai_bot.tool_results.artifacts as module

    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "artifact", retention_seconds=60)
    if operation in {"image", "inspect"}:
        handle = await store.write_media_artifact(
            provider_id="plugin", tool_name="capture", images=_result().images, access=access
        )
    else:
        handle = await store.write_artifact(
            provider_id="plugin",
            tool_name="read",
            content='{"secret":"value"}',
            media_type="application/json",
            access=access,
        )
    authorize = AsyncMock(wraps=store._authorized)
    monkeypatch.setattr(store, "_authorized", authorize)
    original_io = module.asyncio.to_thread

    async def read_then_reset(function, *args, **kwargs):
        result = await original_io(function, *args, **kwargs)
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == access.conversation_id)
                .values(generation=2)
            )
        return result

    monkeypatch.setattr(module.asyncio, "to_thread", read_then_reset)
    result = await store.read(handle, operation=operation, access=access)
    assert result["error_code"] == "artifact_not_authorized"
    assert authorize.await_count == 2
    assert "value" not in str(result) and "base64" not in str(result)


async def test_cleanup_uses_one_mark_and_one_final_batch_with_failed_unlink(
    database, tmp_path, monkeypatch
):
    from pathlib import Path

    store = ToolArtifactRepository(database, tmp_path / "artifact", retention_seconds=60)
    handles = [
        await store.write_artifact(
            provider_id="core", tool_name="read", content="body", media_type="text/plain"
        )
        for _ in range(3)
    ]
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ToolArtifactModel).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    writers = 0
    original_writer = database.immediate_session

    @asynccontextmanager
    async def count_writer():
        nonlocal writers
        writers += 1
        async with original_writer() as session:
            yield session

    monkeypatch.setattr(database, "immediate_session", count_writer)
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name.startswith(handles[0]):
            raise PermissionError("temporary file sharing")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert await store.cleanup() == 2
    assert writers == 2
    async with database.sessions() as session:
        rows = tuple(await session.scalars(select(ToolArtifactModel)))
    assert len(rows) == 1 and rows[0].handle_id == handles[0] and rows[0].deleting
    assert (await store.read(handles[0])) is None
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert await store.cleanup() == 1
    assert writers == 4


async def test_rollup_real_conversation_ids_with_same_integer_prefix_run_independently():
    import asyncio
    from dataclasses import replace

    from tests.unit.test_rollup_complete_sources import candidate

    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.conversation.rollup.service import ConversationRollupService

    service = ConversationRollupService(models=None, config=RollupPolicyConfig(), timeout_seconds=5)
    entered = []
    release = asyncio.Event()

    async def model(item, *, required=False):
        entered.append(item.conversation_id)
        await release.wait()
        return "approved summary"

    service._model_summary = model
    ids = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002")
    tasks = [
        asyncio.create_task(service.summarize_candidate(replace(candidate(), conversation_id=id)))
        for id in ids
    ]
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if len(entered) == 2:
                break
        assert set(entered) == set(ids)
        assert set(service._active) == {(id, 1) for id in ids}
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, tuple) for result in results)
    assert not service._active


async def test_media_and_emoji_upserts_return_records_without_followup_select(database):
    from sqlalchemy import event

    from qq_ai_bot.persistence.media_repository import (
        EmojiDescriptionRepository,
        MediaAnalysisRepository,
    )

    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    now = datetime.now(UTC)
    media = MediaAnalysisRepository(database)
    emoji = EmojiDescriptionRepository(database)
    try:
        common = dict(
            analysis_mode="general",
            question_hash=None,
            provider="first",
            model="model",
            prompt_version="v1",
            observation_json={"overall_description": "one"},
        )
        first = await media.save(
            source_event_id=None,
            segment_index=0,
            content_hash="a" * 64,
            expires_at=now + timedelta(hours=1),
            **common,
        )
        second = await media.save(
            source_event_id=None,
            segment_index=9,
            content_hash="a" * 64,
            expires_at=now + timedelta(hours=2),
            **{**common, "provider": "refresh"},
        )
        original = await emoji.save_many(("emoji:one", "emoji:two"), now=now, **common)
        refreshed = await emoji.save_many(
            ("emoji:one", "emoji:two"),
            now=now + timedelta(seconds=1),
            **{**common, "provider": "refresh"},
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert first.id == second.id and second.segment_index == 0 and second.provider == "refresh"
    assert [row.id for row in original] == [row.id for row in refreshed]
    assert all(row.provider == "refresh" and row.hit_count == 0 for row in refreshed)
    relevant = [sql for sql in statements if "media_analyses" in sql or "emoji_descriptions" in sql]
    assert len(relevant) == 6 and all("RETURNING" in sql for sql in relevant)


@pytest.mark.parametrize("error", [RuntimeError("diagnostic unavailable"), ValueError("serialize")])
async def test_successful_delivery_closes_memory_session_when_statistics_fail(error, caplog):
    from types import SimpleNamespace

    from qq_ai_bot.services.chat import ChatService

    session = SimpleNamespace(on_delivery_confirmed=AsyncMock(side_effect=error), close=AsyncMock())
    await ChatService._finish_memory_turn(
        None,
        session,
        run_id="original-send",
        delivered_text="sent",
        delivered=True,
        cancelled=False,
    )
    session.close.assert_awaited_once()
    assert session.on_delivery_confirmed.call_args.args[0].status.value == "complete"
    assert "coverage_incomplete=true" in caplog.text


async def test_embedding_lookup_failure_keeps_original_committed_receipt(database, monkeypatch):
    from tests.unit.test_memory_mutation import _context, _event, _service

    from qq_ai_bot.memory.enums import MemoryScopeType
    from qq_ai_bot.memory.mutation.models import (
        MemoryMutationOperation,
        MemoryMutationRequest,
        MemoryMutationTarget,
    )

    service, facts, ledger, _processor = _service(database)
    source = await _event(
        ledger, message_id="embedding-diagnostic", sender_user_id="1001", content="我现在住在上海"
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
        ),
        new_content="我现在住在上海",
        memory_key="location:home",
        category="location",
        reason="用户明确说明",
        confidence=0.9,
    )
    original = facts.get_fact
    monkeypatch.setattr(facts, "get_fact", AsyncMock(side_effect=RuntimeError("diagnostic read")))
    result = await service.mutate(request, _context(source))
    assert result.ok and result.mutation_id and result.new_fact_id
    monkeypatch.setattr(facts, "get_fact", original)
    replay = await service.mutate(request, _context(source))
    assert replay.mutation_id == result.mutation_id and replay.new_fact_id == result.new_fact_id
    assert len(await facts.list_person("1001")) == 1


async def test_participation_one_tick_reuses_history_and_social_window(database, monkeypatch):
    from tests.unit.test_participation_feedback import setup

    from qq_ai_bot.services import participation_feedback
    from qq_ai_bot.services.semantic_participation import SemanticParticipationService

    service, item, _run, _task = await setup(database)
    service._scene = AsyncMock(return_value=item.scene)
    service._dirty = {item.scene.conversation_id: {1: True}}

    async def hydrate(*args):
        service._dirty[item.scene.conversation_id][2] = False
        return object()

    service._hydrate = AsyncMock(side_effect=hydrate)
    service._validate_boundaries = AsyncMock()
    service._binding = AsyncMock(side_effect=RuntimeError("prepared boundary"))
    reads = AsyncMock(wraps=participation_feedback._scope_social_rows)
    monkeypatch.setattr(participation_feedback, "_scope_social_rows", reads)
    with pytest.raises(RuntimeError, match="prepared boundary"):
        await SemanticParticipationService._advance_scene(service, item, direct={1: True})
    assert reads.await_count == 1 and service._hydrate.await_count == 1
    assert service._dirty[item.scene.conversation_id] == {2: False}


async def test_rollup_signals_preserve_failed_deadline_and_wake_policy_park(database):
    from tests.unit.test_conversation_rollup_370 import _append_v2, _policy, _prepare_v2_private

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationRollupJobModel
    from qq_ai_bot.conversation.canonical_rollup import signal_canonical_rollup_if_needed
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    policy = _policy()
    scope = await _prepare_v2_private(database, bot="80001", peer="10001")
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    await _append_v2(uow, scope, 4)
    repository = ConversationRollupRepository(database, policy)
    claim = await repository.claim_next_job(lease_owner="original", lease_seconds=30)
    deadline = datetime.now(UTC) + timedelta(minutes=16)
    async with database.sessions() as session, session.begin():
        job = await session.get(CanonicalConversationRollupJobModel, claim.conversation_id)
        job.next_attempt_at = deadline
        job.failure_count = 172
        job.last_error_category = "model_invalid_candidate"
        scene = await session.get(CanonicalConversationModel, claim.conversation_id)
        for _ in range(3):
            await signal_canonical_rollup_if_needed(session, scene, policy, force_existing=True)
        assert job.next_attempt_at == deadline and job.failure_count == 172
        assert job.signal_revision == claim.claimed_signal_revision + 3
        job.last_error_category = "llm_origin_ineligible"
        await signal_canonical_rollup_if_needed(session, scene, policy, force_existing=True)
        assert job.next_attempt_at < deadline


async def test_ordinary_result_capture_does_not_forge_work_artifact_owner(database, tmp_path):
    from qq_ai_bot.runtime.effect_outcomes import ResultCapture, current_result_capture

    store = ToolArtifactRepository(database, tmp_path / "ordinary", retention_seconds=60)
    token = current_result_capture.set(ResultCapture("", ""))
    try:
        handle = await store.write_artifact(
            provider_id="core", tool_name="read", content="facts", media_type="text/plain"
        )
    finally:
        current_result_capture.reset(token)
    async with database.sessions() as session:
        row = await session.get(ToolArtifactModel, handle)
        assert row.work_id is None and row.effect_key is None
    assert (await store.read(handle))["content"] == "facts"
