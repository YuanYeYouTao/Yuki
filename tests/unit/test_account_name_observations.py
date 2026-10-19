from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, MessageMention, SenderIdentity
from yuki.identity.canonical_repository import ensure_person, ensure_space, find_space_binding
from yuki.identity.db_models import AccountModel, AccountNameObservationModel, CanonicalPersonModel
from yuki.identity.person_state import prepare_message_account_observations


@pytest.mark.asyncio
async def test_unknown_native_mention_records_account_without_creating_person(database):
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        await ensure_person(session, "10001")
        await ensure_space(session, "20001")
    async with database.sessions() as session:
        original_count = await session.scalar(
            select(func.count()).select_from(CanonicalPersonModel)
        )
    message = InboundMessage(
        "original",
        "message:group",
        ScopeType.GROUP,
        SenderIdentity("10001", nickname="Sender", nickname_known=True),
        "@Unknown",
        group_id="20001",
        mentions=(MessageMention("member", 0, 0, 8, "90002", "Unknown"),),
        segments=({"type": "at", "data": {"qq": "90002", "name": "Unknown"}},),
    )
    async with database.sessions() as session, session.begin():
        binding = await find_space_binding(session, "20001")
        prepared, apply = await prepare_message_account_observations(
            session, message, space_binding_id=binding.id, now=now
        )
        assert not session.new
        assert prepared.mentions[0].account_id
        assert prepared.mentions[0].person_id is None
        apply()
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(CanonicalPersonModel))
            == original_count
        )
        target = await session.scalar(
            select(AccountModel).where(AccountModel.external_account_id == "90002")
        )
        observation = await session.scalar(
            select(AccountNameObservationModel).where(
                AccountNameObservationModel.account_id == target.id
            )
        )
        assert observation.name == "Unknown"
        assert observation.assignment_id is None
