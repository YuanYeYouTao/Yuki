"""Personal Work cards retain their original entry after native group migration."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_client_works import owner as client_owner

from yuki.client_api.works import ClientWorks
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import (
    create_space_binding,
    ensure_account,
    replace_assignment,
)
from yuki.identity.db_models import CanonicalPersonModel, SpaceBindingModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_repository import WorkRepository


async def test_work_card_keeps_original_space_binding_after_telegram_group_migration(
    app_env, telegram_env, database
):
    device, _ = await client_owner(app_env, database)
    principal = await app_env.identity.authenticate(device["token"])
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        owner = await writer.get(CanonicalPersonModel, principal.person_id)
        account = await ensure_account(writer, "42", platform="telegram", now=now)
        await replace_assignment(writer, account, None, owner, now=now)
        binding = await create_space_binding(
            writer,
            external_space_id="-42",
            platform="telegram",
            name="Original group",
            enabled=True,
            autonomous_enabled=True,
            require_mention=False,
            now=now,
        )
        original_id = binding.id
        conversation = await ensure_canonical_conversation(
            writer,
            kind="space",
            primary_scope_key="telegram:bot:80001:group:-42",
            space_id=binding.space_id,
            space_binding_id=binding.id,
        )
    original = (
        await seed_scoped_event(
            telegram_env.harness.scoped_events,
            scope=ConversationScope.group(
                "80001",
                "-42",
                platform="telegram",
                presence_id=telegram_env.gateway.presence_id,
                conversation_id=conversation.conversation_id,
            ),
            platform_message_id=str(uuid4()),
            sender_user_id="42",
            direction="inbound",
            content="Original accepted Work source",
        )
    ).event
    repository = WorkRepository(database)
    lease = await repository.acquire(original.canonical_conversation_id, 1)
    try:
        accepted = await repository.accept(
            lease,
            source_key=str(uuid4()),
            goal="Original accepted goal",
            source={
                "actor_person_id": principal.person_id,
                "principal_kind": "person",
                "trigger_event_id": original.id,
                "presence_id": original.ingress_presence_id,
                "conversation_id": original.canonical_conversation_id,
            },
        )
    finally:
        await repository.release(lease)
    works = ClientWorks(app_env.identity, app_env.gateway)
    before = await works.get(principal, accepted["id"])
    assert before["source"]["binding_id"] == original_id

    await telegram_env.gateway.receive(
        {"update_id": 1, "message": {"chat": {"id": -42}, "migrate_to_chat_id": -10042}}
    )

    after = await works.get(principal, accepted["id"])
    assert after == before
    async with database.read_session() as reader:
        entry = await reader.get(SpaceBindingModel, original_id)
        assert entry.external_space_id == "-10042"
        current = await reader.get(CanonicalConversationModel, original.canonical_conversation_id)
        assert current.space_binding_id == original_id
        event = await reader.get(ChatEventModel, original.id)
        assert event.group_id == "-42" and event.author_person_id == principal.person_id
        assert (
            await reader.scalar(select(ChatEventModel.id).where(ChatEventModel.id > original.id))
            is None
        )
    assert not telegram_env.model.requests and not telegram_env.gateway._turns
