"""Privacy erasure discovers account mentions hidden by valid JSON escapes."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.models import AdminOperationEventModel, ChatEventModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository


@pytest.mark.parametrize("surface", ["event", "audit"])
async def test_forget_discovers_json_escaped_alias_without_plaintext_mention(database, surface):
    escaped_alias = r"\u0031\u0030\u0030\u0031"
    if surface == "event":
        event, _ = await EventLedgerRepository(database).append(
            bot_user_id="9999",
            platform_message_id="escaped-privacy",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            sender_user_id="1002",
            direction="inbound",
            content="shared neutral text",
            segments=(),
        )
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == event.id)
                .values(segments_json='[{"type":"at","data":{"qq":"' + escaped_alias + '"}}]')
            )
    else:
        async with database.immediate_session() as writer:
            writer.add(
                AdminOperationEventModel(
                    actor_user_id="1002",
                    capability="test",
                    operation="test",
                    target_type="test",
                    before_json='{"owner":"' + escaped_alias + '","keep":true}',
                    after_json="null",
                    success=True,
                    duration_seconds=0,
                    created_at=datetime.now(UTC),
                )
            )

    assert await PeopleRepository(database).delete_person("1001")
    async with database.sessions() as reader:
        if surface == "event":
            remaining = await reader.get(ChatEventModel, event.id)
            assert remaining is not None
            assert remaining.content == "shared neutral text"
            assert json.loads(remaining.segments_json)[0]["data"]["qq"] == "[已删除用户]"
        else:
            audit = await reader.scalar(select(AdminOperationEventModel))
            assert audit.actor_user_id == "1002"
            assert json.loads(audit.before_json) == {"owner": "[已删除用户]", "keep": True}
