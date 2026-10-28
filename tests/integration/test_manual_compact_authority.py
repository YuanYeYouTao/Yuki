"""Manual group compact rechecks its original Person at actual model admission."""

from dataclasses import replace

from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.domain.conversations import ConversationScope
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.fake import FakeLLMProvider
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState


async def test_group_compact_revocation_after_context_preparation_buys_no_request(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, env.person)).is_superuser = True
    provider = FakeLLMProvider(lambda _: "summary that must not be purchased")
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001"), provider
    )
    chat = harness.processor._chat
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    await seed_scoped_event(
        harness.scoped_events,
        scope=ConversationScope.group("80001", "20001"),
        origin_key="prior-public",
        sender_user_id="10001",
        direction="inbound",
        content="Earlier public decision.",
    )
    prepare = chat._build_messages
    revoked = False

    async def prepare_then_revoke(*args, **kwargs):
        nonlocal revoked
        result = await prepare(*args, **kwargs)
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, env.person)).is_superuser = False
        revoked = True
        return result

    monkeypatch.setattr(chat, "_build_messages", prepare_then_revoke)
    command = replace(
        inbound(
            "/ai compact", message_id="manual-group-compact", user_id="10001", group_id="20001"
        ),
        bot_user_id="80001",
        person_id=env.person,
        space_id=env.space,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
    )
    try:
        await harness.processor.handle(command, MemorySender())
    except PermissionError as error:
        assert str(error) == "manual_public_compaction_requires_current_superuser"
    assert revoked
    assert provider.requests == []
