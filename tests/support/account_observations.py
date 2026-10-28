"""Seed accepted account metadata through the production observation preparers."""

from datetime import UTC, datetime

from sqlalchemy import text

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.domain.profiles import sanitize_profile_name
from yuki.identity.canonical_repository import find_identity_binding, find_space_binding
from yuki.identity.person_state import (
    prepare_canonical_person_observation,
    prepare_message_account_observations,
)


def profile_message(
    *,
    user_id: str,
    nickname: str,
    group_id: str | None = None,
    group_card: str = "",
    group_name: str = "",
    nickname_known: bool = True,
    group_card_known: bool = True,
    platform: str = "qq",
) -> InboundMessage:
    return InboundMessage(
        ingress_key="accepted-profile-fixture",
        native_message_id="accepted-profile-fixture",
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
        platform=platform,
        bot_user_id="8000",
        sender=SenderIdentity(
            user_id,
            nickname=nickname,
            group_card=group_card,
            nickname_known=nickname_known,
            group_card_known=group_card_known,
        ),
        text="",
        group_id=group_id,
        group_name=group_name,
    )


async def apply_observations(database, message: InboundMessage) -> None:
    """The fixture supplies an already accepted human source; it does not admit one."""
    now = datetime.now(UTC)
    async with database.read_session() as reader:
        await reader.execute(text("BEGIN"))
        binding = await find_identity_binding(
            reader, message.sender.user_id, platform=message.platform
        )
        assert binding is not None, "seed the original accepted Person Assignment first"
        space = (
            await find_space_binding(reader, message.group_id, platform=message.platform)
            if message.group_id is not None
            else None
        )
        _, accounts = await prepare_message_account_observations(
            reader,
            message,
            space_binding_id=space.id if space else None,
            assignment_id=binding.id,
            now=now,
        )
        person = prepare_canonical_person_observation(
            binding=binding,
            nickname=sanitize_profile_name(message.sender.nickname),
            group_name=sanitize_profile_name(message.group_name),
            space_id=space.space_id if space else None,
            space_binding_id=space.id if space else None,
            now=now,
        )
    async with database.immediate_session() as writer:
        apply_accounts = await accounts(writer)
        apply_person = await person(writer)
        apply_accounts()
        apply_person()
