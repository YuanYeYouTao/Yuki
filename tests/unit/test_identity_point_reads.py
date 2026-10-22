"""Identity preparation reads its actual keys rather than unrelated account history."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert
from tests.support.social_identity_cases import social_env
from tests.support.sqlite_cost import capture_reads, read_vm_steps

from yuki.conversation.hydrate import binding_for_scope
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.canonical_repository import find_identity_binding, find_space_binding
from yuki.identity.db_models import (
    AccountAssignmentModel,
    AccountModel,
    AccountNameObservationModel,
)
from yuki.identity.person_state import prepare_message_account_observations


@pytest.mark.parametrize("operation", ["names", "private-binding", "person-route", "space-route"])
async def test_fixed_identity_read_does_not_expand_unrelated_names_or_accounts(
    database, tmp_path, operation
):
    env = await social_env(database, tmp_path)
    now = datetime.now(UTC)
    async with database.read_session() as reader:
        sender = await find_identity_binding(reader, "10001")
        other = await find_identity_binding(reader, "10002")
        space_binding = await find_space_binding(reader, "20001")
        source_account, source_stage = sender.account_id, sender.id
        other_person, space_binding_id = other.person_id, space_binding.id
    message = InboundMessage(
        message_id="point-read",
        event_type="message:group",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001", nickname="current-name", nickname_known=True),
        text="ordinary message",
        bot_user_id="80001",
        group_id="20001",
        occurred_at=now,
    )
    costs, previous = [], 0
    for size in (8, 512):
        async with database.immediate_session() as writer:
            if operation == "names":
                await writer.execute(
                    insert(AccountNameObservationModel),
                    [
                        dict(
                            account_id=source_account,
                            assignment_id=source_stage,
                            space_binding_id=None,
                            name=f"old-name-{index}",
                            kind="nickname",
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                        for index in range(previous, size)
                    ],
                )
            else:
                accounts = [
                    dict(
                        id=str(uuid4()),
                        platform="qq",
                        external_account_id=f"unrelated-{index}",
                        display_name="unrelated",
                        created_at=now,
                        updated_at=now,
                    )
                    for index in range(previous, size)
                ]
                await writer.execute(insert(AccountModel.__table__), accounts)
                await writer.execute(
                    insert(AccountAssignmentModel.__table__),
                    [
                        dict(
                            id=str(uuid4()),
                            account_id=account["id"],
                            kind="person",
                            person_id=other_person,
                            status="active",
                            started_at=now,
                            first_seen_at=now,
                            last_seen_at=now,
                            created_at=now,
                            updated_at=now,
                            revision=1,
                            ingest_eligible=True,
                        )
                        for account in accounts
                    ],
                )
        with capture_reads(database) as reads:
            if operation == "names":
                async with database.read_session() as reader:
                    prepared, _apply = await prepare_message_account_observations(
                        reader,
                        message,
                        space_binding_id=space_binding_id,
                        now=now,
                        assignment_id=source_stage,
                    )
                    assert prepared.sender == message.sender
            elif operation == "private-binding":
                async with database.read_session() as reader:
                    binding = await binding_for_scope(
                        reader, ConversationScope.private("80001", "10001")
                    )
                    assert binding.id == source_stage
            elif operation == "person-route":
                candidates = await env.router._person_candidates(env.person)
                assert [(row.presence_id, row.binding_id) for row in candidates] == [
                    (env.presence, source_stage)
                ]
            else:
                candidates = await env.router._space_candidates(env.space)
                assert [(row.presence_id, row.binding_id) for row in candidates] == [
                    (env.presence, space_binding_id)
                ]
        costs.append(read_vm_steps(database, reads))
        previous = size
    assert costs[1] < costs[0] * 2, costs
