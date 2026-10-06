"""A maintenance target cannot replace complete-source and real-capacity fences."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from tests.conftest import make_settings
from tests.unit.rollup_test_helpers import model_summary

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError, RollupSourceChangedError
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.model_runtime.capacity import estimate_text_tokens
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel, PluginInstallationModel
from qq_ai_bot.runtime.context_preparation import (
    ContextPreparationMode,
    ContextRollupRequired,
    context_preparation_mode,
)
from qq_ai_bot.runtime.trigger import SelfInitiativeTrigger
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError


class _SummaryModel:
    def __init__(self):
        self.requests = []

    async def execute(self, _task, request, **_kwargs):
        self.requests.append(request)
        return ChatResponse(content=model_summary(request, "retained facts"), latency_seconds=0)


@pytest.mark.asyncio
async def test_plugin_history_expands_small_prefetch_without_auxiliary_model(database):
    from dataclasses import replace

    from tests.unit.test_commands_and_chat import inbound

    from qq_ai_bot.time.models import TimeContext

    assembler, repository, model, arguments = await _history(database, read_budget=1, hold=False)
    original = await repository.load_prompt_snapshot(ConversationScope.group("8000", "2001"))
    assert not original.raw_complete
    now = datetime.now(UTC)
    assembled = await assembler.assemble_plugin(
        inbound=replace(
            inbound("plugin request", group_id="2001", message_id="plugin-prefetch"),
            bot_user_id="8000",
        ),
        content="plugin request",
        metadata={},
        current_time=TimeContext(now, now, "UTC"),
        read_history=True,
        projection_scope="plugin",
        runtime=arguments["runtime"],
    )
    assert len(assembled.visible_event_ids) == 6
    assert all(f"record-{index}:" in str(assembled.history_messages) for index in range(6))
    assert model.requests == []


@pytest.mark.parametrize("character", ["x", "猫"], ids=["ascii-fits", "cjk-over-capacity"])
def test_required_metadata_fallback_uses_tokens_without_expanding_optional(character):
    scene = {"required": character * 60000}
    context = {
        "scene": scene,
        "current_self": {"facts": [{"fact_id": 42, "text": "optional fact"}]},
    }
    expected = {"items": [{"id": "scene", "data": scene}]}
    serialized = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    hard_tokens = 25000
    assert len(serialized) > hard_tokens
    if character == "x":
        assert estimate_text_tokens(serialized) < hard_tokens
        payload, fact_ids = ContextAssembler._fit_metadata(context, 512, capacity_limit=hard_tokens)
        assert payload == expected and fact_ids == ()
    else:
        assert estimate_text_tokens(serialized) > hard_tokens
        with pytest.raises(ValueError, match="required context exceeds configured budget"):
            ContextAssembler._fit_metadata(context, 512, capacity_limit=hard_tokens)


async def _history(
    database,
    *,
    hold=True,
    capacity=5000,
    read_budget=10000,
    instruction="continue",
    soft_budget=1000,
):
    now = datetime.now(UTC)
    policy = RollupPolicyConfig(context_token_budget=read_budget)
    identity = ConversationScope.group("8000", "2001")
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "8000")
        space = await ensure_space(writer, "2001")
        writer.add(
            PluginInstallationModel(
                plugin_id="soft-history",
                name="test source hold",
                version="1",
                plugin_api="3.1",
                yuki_requires="*",
                manifest_hash="a" * 64,
                entrypoint="test.py",
                status="running",
                enabled=True,
                discovered_at=now,
                updated_at=now,
            )
        )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    external = await uow.append_external(
        scope=identity,
        platform_message_id="external-source",
        source_plugin_id="soft-history",
        external_source="test",
        external_event_key="original-source",
        external_event_type="test",
        external_payload={},
        external_target_id="2001",
        content="original external source",
        occurred_at=now,
    )
    for index in range(6):
        await uow.append(
            scope=identity,
            platform_message_id=f"history-{index}",
            sender_user_id="8000",
            direction="outbound",
            sender_is_bot=True,
            content=f"record-{index}:" + "x" * 600,
            occurred_at=now,
        )
    if hold:
        async with database.immediate_session() as writer:
            writer.add(
                PluginBackgroundTurnJobModel(
                    source_event_id=external.event.id,
                    plugin_id="soft-history",
                    target_type="group",
                    target_id="2001",
                    bot_user_id="8000",
                    status="pending",
                    canonical_target_space_id=space,
                    canonical_conversation_id=external.event.canonical_conversation_id,
                    canonical_presence_id=presence,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
    repository = ConversationRollupRepository(database, policy)
    model = _SummaryModel()
    service = ConversationRollupService(models=model, config=policy, timeout_seconds=1)
    settings = make_settings(
        database.url,
        conversation_rollup_foreground_max_batches=1,
        context_metadata_budget_ratio=0.2,
    )
    assembler = ContextAssembler(
        settings=settings,
        ledger=EventLedgerRepository(database),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=repository,
        rollup_service=service,
        history_budget=lambda _runtime: soft_budget,
        history_capacity=lambda _runtime: capacity,
    )
    runtime_service = RuntimeConfigService(settings=settings, database=database)
    await runtime_service.initialize()
    runtime = await runtime_service.snapshot(group_id="2001")
    trigger = SelfInitiativeTrigger(
        run_id="soft-history",
        conversation_id=external.event.canonical_conversation_id,
        generation=external.scope.generation,
        space_id=space,
        presence_id=presence,
        group_id="2001",
        bot_user_id="8000",
        instruction=instruction,
    )
    turn = ConversationTurnSnapshot(
        external.scope.id,
        identity.key,
        external.scope.generation,
        None,
        1,
        initiative_run_id=trigger.run_id,
    )
    return assembler, repository, model, dict(trigger=trigger, runtime=runtime, turn=turn)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [ContextPreparationMode.FOREGROUND, ContextPreparationMode.DURABLE]
)
async def test_real_source_hold_preserves_complete_raw_above_soft_target(database, mode):
    assembler, repository, model, arguments = await _history(database)
    scope = ConversationScope.group("8000", "2001")
    original = await repository.load_prompt_snapshot(scope)
    assert original.raw_complete and len(original.raw_events) == 6
    claim = await repository.claim_scope_for_foreground(
        scope, lease_owner="hold-proof", lease_seconds=30
    )
    assert claim is not None
    async with database.sessions() as reader:
        assert (
            await repository._coverage_holds.earliest_source_event_id(
                reader, canonical_conversation_id=claim.conversation_id
            )
            == 1
        )
    assert await repository.candidate_for_claim(claim, token_budget=1000) is None
    await repository.release_owner("hold-proof")
    token = context_preparation_mode.set(mode)
    try:
        assembled = await assembler.assemble_self_initiative(**arguments)
    finally:
        context_preparation_mode.reset(token)
    assert assembled.visible_event_ids == frozenset(row.id for row in original.raw_events)
    assert all(
        row.content in "\n".join(item.content or "" for item in assembled.history_messages)
        for row in original.raw_events
    )
    assert assembled.rollup_text == "" and not assembled.metrics.raw_history_window_shifted
    assert model.requests == []
    assert (await repository.load_prompt_snapshot(scope)) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [ContextPreparationMode.FOREGROUND, ContextPreparationMode.DURABLE]
)
async def test_real_capacity_failure_with_hold_still_requires_coverage(database, mode):
    assembler, _repository, model, arguments = await _history(database, capacity=500)
    token = context_preparation_mode.set(mode)
    try:
        error = (
            ContextRollupRequired
            if mode is ContextPreparationMode.DURABLE
            else ConversationCoverageError
        )
        with pytest.raises(error):
            await assembler.assemble_self_initiative(**arguments)
    finally:
        context_preparation_mode.reset(token)
    assert model.requests == []


@pytest.mark.asyncio
async def test_small_prefetch_is_completed_without_waiting_for_summary(database):
    assembler, repository, model, arguments = await _history(database, read_budget=1)
    original = await repository.load_prompt_snapshot(ConversationScope.group("8000", "2001"))
    assert not original.raw_complete and len(original.raw_events) == 1
    assembled = await assembler.assemble_self_initiative(**arguments)
    assert len(assembled.visible_event_ids) == 6
    assert all(
        f"record-{index}:"
        in "\n".join(message.content or "" for message in assembled.history_messages)
        for index in range(6)
    )
    assert model.requests == []


@pytest.mark.asyncio
async def test_tiny_soft_policy_keeps_required_scene_and_complete_source(database, monkeypatch):
    assembler, _repository, model, arguments = await _history(database, soft_budget=1)
    maintenance = AsyncMock(
        side_effect=AssertionError("soft target is below required current input")
    )
    monkeypatch.setattr(assembler._rollup_service, "ensure_required_coverage", maintenance)
    assembled = await assembler.assemble_self_initiative(**arguments)
    assert assembled.metadata_payload["items"] == [
        {
            "id": "scene",
            "data": {
                "type": "group",
                "group_id": "2001",
                "trigger": "self_initiative",
                "current_actor": "SELF",
            },
        }
    ]
    assert len(assembled.visible_event_ids) == 6
    assert "continue" in assembled.current_message.content
    maintenance.assert_not_awaited()
    assert model.requests == []


@pytest.mark.asyncio
async def test_current_input_above_soft_floor_uses_real_reserve_without_durable_wait(
    database, monkeypatch
):
    assembler, _repository, model, arguments = await _history(database, instruction="c" * 3600)
    maintenance = AsyncMock(side_effect=AssertionError("current input cannot meet soft target"))
    monkeypatch.setattr(assembler._rollup_service, "ensure_required_coverage", maintenance)
    token = context_preparation_mode.set(ContextPreparationMode.DURABLE)
    try:
        assembled = await assembler.assemble_self_initiative(**arguments)
    finally:
        context_preparation_mode.reset(token)
    assert "c" * 3600 in assembled.current_message.content
    assert len(assembled.visible_event_ids) == 6
    maintenance.assert_not_awaited()
    assert model.requests == []


@pytest.mark.asyncio
async def test_compressible_history_above_soft_target_does_not_start_foreground_model(
    database,
):
    assembler, repository, model, arguments = await _history(
        database, hold=False, instruction="c" * 1600
    )
    assembled = await assembler.assemble_self_initiative(**arguments)
    current = await repository.load_prompt_snapshot(ConversationScope.group("8000", "2001"))
    assert model.requests == [] and current.effective_coverage == 0
    assert assembled.rollup_text == ""
    assert assembled.visible_event_ids == frozenset(row.id for row in current.raw_events)
    assert current.raw_complete and not assembled.metrics.raw_history_window_shifted
    assert (
        assembler._uncovered_tokens(
            assembler._uncovered_prompt_view(
                current.raw_events,
                current_event_id=None,
                content=arguments["trigger"].instruction,
                yuki_account_ids=frozenset({"8000"}),
                current_message_override=assembled.current_message,
                current_event=None,
            ),
            assembled.rollup_text,
        )
        > 1000
    )


@pytest.mark.asyncio
async def test_source_edit_during_soft_attempt_is_rejected_by_actual_read_version(
    database, monkeypatch
):
    assembler, _repository, _model, arguments = await _history(database)

    original_read = assembler._load_history_snapshot

    async def change_source(*args, **kwargs):
        snapshot = await original_read(*args, **kwargs)
        async with database.immediate_session() as writer:
            row = await writer.scalar(
                select(ChatEventModel).where(ChatEventModel.event_kind == "message")
            )
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == row.id)
                .values(content="changed source")
            )
        return snapshot

    monkeypatch.setattr(assembler, "_load_history_snapshot", change_source)
    with pytest.raises(HistorySourceChangedError):
        await assembler.assemble_self_initiative(**arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RollupSourceChangedError("source changed"),
        ConversationCoverageError("rollup_required_settlement_timeout"),
    ],
)
async def test_soft_capacity_fallback_never_swallows_source_or_settlement_errors(
    database, monkeypatch, error
):
    assembler, _repository, _model, arguments = await _history(database, capacity=500)
    monkeypatch.setattr(
        assembler._rollup_service, "ensure_required_coverage", AsyncMock(side_effect=error)
    )
    with pytest.raises(type(error), match=str(error)):
        await assembler.assemble_self_initiative(**arguments)
