"""Persistence policies keep history watermarks independent of request capacity."""

from datetime import UTC, datetime

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.persistence import PersistenceModule
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.identity import SpaceId
from qq_ai_bot.identity.db_models import CanonicalSpaceModel


@pytest.mark.parametrize(
    "hard_window,soft_window", [(40000, 40000), (96000, 90000), (524288, 90000)]
)
def test_initial_rollup_watermark_respects_soft_and_hard_windows(
    database, hard_window, soft_window
):
    settings = make_settings(database.url, context_window_tokens=hard_window)
    bundle = PersistenceModule(settings, lifecycle=LifecycleRegistry(), database=database).build()
    policy = bundle.conversation_rollups.config
    assert policy.context_token_budget == soft_window
    assert policy.context_token_budget * policy.trigger_ratio == soft_window * 0.9
    assert policy.context_token_budget * policy.target_ratio == soft_window * 0.6
    assert settings.context_window_tokens == hard_window
    assert settings.context_compaction_window_tokens == 90000


@pytest.mark.asyncio
async def test_hot_rollup_policy_reads_scope_soft_window_without_mutating_base(database):
    settings = make_settings(database.url, context_window_tokens=524288)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    bundle = PersistenceModule(
        settings, lifecycle=LifecycleRegistry(), database=database, runtime_config=runtime
    ).build()
    first, second = SpaceId.new(), SpaceId.new()
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add_all(
            CanonicalSpaceModel(
                id=space.text, revision=1, enabled=True, created_at=now, updated_at=now
            )
            for space in (first, second)
        )
    repository = bundle.conversation_rollups
    first_scope = ConversationScope.group("80001", first.text)
    second_scope = ConversationScope.group("80001", second.text)
    original = await repository._scope_policy(first_scope)
    assert original.context_token_budget == 90000
    changes = (
        ("context.compaction_window_tokens", 60000),
        ("context.compaction_trigger_ratio", 0.8),
        ("context.compaction_target_ratio", 0.5),
    )
    for key, value in changes:
        outcome = await runtime.set_override(
            key,
            value,
            scope_type="group",
            scope_id=first.text,
            actor_user_id="test-operator",
            trigger_message_id="",
        )
        assert outcome.success
    updated = await repository._scope_policy(first_scope)
    unaffected = await repository._scope_policy(second_scope)
    assert updated.context_token_budget == 60000
    assert updated.context_token_budget * updated.trigger_ratio == 48000
    assert updated.context_token_budget * updated.target_ratio == 30000
    assert unaffected.context_token_budget == 90000
    assert original.context_token_budget == 90000
    assert repository.config.context_token_budget == 90000
    assert (await runtime.snapshot(group_id=first.text)).context.window_tokens == 524288
    outcome = await runtime.set_override(
        "context.window_tokens",
        40000,
        scope_type="group",
        scope_id=first.text,
        actor_user_id="test-operator",
        trigger_message_id="",
    )
    assert outcome.success
    limited = await repository._scope_policy(first_scope)
    assert limited.context_token_budget == 40000
    assert limited.trigger_ratio == 0.8
    assert (await runtime.snapshot(group_id=first.text)).context.compaction_window_tokens == 60000
