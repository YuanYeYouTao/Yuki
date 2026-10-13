"""Known preparation failures leave the five-item report queue."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker
from qq_ai_bot.persistence.models import ChatEventModel, MemorySelfReflectionCycleModel


@pytest.mark.parametrize("failure", ["source_missing", "target_disabled"])
async def test_failed_reports_settle_and_allow_later_real_delivery(database, tmp_path, failure):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        good = await reader.scalar(select(ChatEventModel))
    bad = await env.service.writer.append(
        scope=ConversationScope.group("80001", "2002"),
        platform_message_id="disabled-report-source",
        sender_user_id="10001",
        direction="inbound",
        content="original manual source",
    )
    bad = bad.event
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel

    async with database.immediate_session() as writer:
        conversation = await writer.get(CanonicalConversationModel, bad.canonical_conversation_id)
        disabled = await writer.get(CanonicalSpaceModel, conversation.space_id)
        disabled.enabled = False
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        for index in range(6):
            writer.add(
                MemorySelfReflectionCycleModel(
                    id=f"report-{index}",
                    source_key=f"manual-{index}",
                    trigger="manual",
                    source_event_id=good.id
                    if index == 5
                    else None
                    if failure == "source_missing"
                    else bad.id,
                    conversation_id=good.canonical_conversation_id
                    if index == 5
                    else bad.canonical_conversation_id,
                    status="completed",
                    created_at=now + timedelta(seconds=index),
                    report_json="{}",
                    delivery_state="pending",
                )
            )
    worker = SelfReflectionWorker(
        settings=make_settings(database.url),
        repository=SelfReflectionRepository(database),
        service=SimpleNamespace(),
    )
    worker.social = env.service
    await worker._deliver_reports()
    async with database.sessions() as reader:
        failed = list(
            await reader.scalars(
                select(MemorySelfReflectionCycleModel).where(
                    MemorySelfReflectionCycleModel.id != "report-5"
                )
            )
        )
        assert len(failed) == 5 and all(row.delivery_state == "failed" for row in failed)
    await worker._deliver_reports()
    async with database.sessions() as reader:
        assert (
            await reader.get(MemorySelfReflectionCycleModel, "report-5")
        ).delivery_state == "delivered"
    sends = sum(action == "send_group_msg" for action, _ in env.bot.calls)
    assert sends == 1
    await worker._deliver_reports()
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == sends


@pytest.mark.parametrize("status", ["uncertain", "executing", "succeeded"])
async def test_delivery_error_preserves_receipt_created_during_preparation(
    database, tmp_path, status
):
    from unittest.mock import AsyncMock

    from qq_ai_bot.memory.self_reflection.reporting import deliver_report
    from qq_ai_bot.social.models import SocialError

    await social_env(database, tmp_path)
    async with database.sessions() as reader:
        source = await reader.scalar(select(ChatEventModel))
    prior = SimpleNamespace(
        status=SimpleNamespace(value=status),
        model_dump=lambda **kwargs: {"status": status},
    )
    social = SimpleNamespace(
        database=database,
        receipts=SimpleNamespace(find=AsyncMock(side_effect=[None, prior])),
        execute=AsyncMock(side_effect=SocialError("route_changed")),
    )
    result = await deliver_report(
        social,
        {
            "id": "original-report",
            "source_event_id": source.id,
            "conversation_id": source.canonical_conversation_id,
            "status": "completed",
            "trigger": "manual",
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    assert result == {"status": status}
    social.execute.assert_awaited_once()
