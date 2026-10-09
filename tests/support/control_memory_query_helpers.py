"""Owner-bound keyset pages and content capability isolation over original facts."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)


@pytest.fixture
async def memory_scene(database, tmp_path):
    env = await social_env(database, tmp_path)
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        source = await session.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                ChatEventModel.direction == "inbound",
            )
        )
        facts = []
        for index in range(36):
            scope = "person" if index < 33 else "group" if index == 33 else "self"
            row = MemoryFactModel(
                scope_type=scope,
                visibility_type="global" if index == 34 else "group" if index == 35 else None,
                canonical_subject_person_id=env.person if scope == "person" else None,
                canonical_subject_space_id=env.space if scope == "group" else None,
                canonical_visibility_space_id=env.space if index == 35 else None,
                kind="fact",
                memory_key=f"private-key-{index}",
                category="fixture",
                content=f"private-fact-{index}",
                normalized_content=f"private-fact-{index}",
                source_type="explicit",
                authority="self_report" if scope != "self" else "agent_reflection",
                status="active",
                created_at=now,
                updated_at=now,
                last_injected_at=None,
            )
            session.add(row)
            facts.append(row)
        await session.flush()
        for row in facts:
            session.add(
                MemoryEvidenceModel(
                    fact_id=row.id,
                    event_id=source,
                    source_speaker_user_id="10001",
                    relation="self_statement",
                    excerpt="private-evidence",
                    created_at=now,
                )
            )
        receipts = []
        for index in range(3):
            row = MemoryToolReceiptModel(
                conversation_key_hash="a" * 64,
                trigger_event_id=source,
                bot_user_id="80001",
                canonical_space_id=env.space,
                provider_id="fixture",
                tool_name="inspect",
                execution_id=f"original-execution-{index}",
                success=True,
                result_excerpt="private-tool",
                result_characters=12,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
            session.add(row)
            receipts.append(row)
        await session.flush()
        for row in receipts:
            session.add(
                MemoryEvidenceModel(
                    fact_id=facts[0].id,
                    tool_receipt_id=row.id,
                    source_speaker_user_id="80001",
                    relation="agent_reflection",
                    excerpt="private-tool-evidence",
                    created_at=now,
                )
            )
    return env, [row.id for row in facts], source, [row.id for row in receipts]
