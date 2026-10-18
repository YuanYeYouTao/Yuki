"""Freeze real mention targets and locally observed names before event publication."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.messages import InboundMessage, render_message_mentions, sanitize_display_name
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import person_root_id
from yuki.persistence.models import ChatEventModel, MembershipModel


async def resolve_message_mentions(
    session: AsyncSession,
    message: InboundMessage,
    *,
    space_id: str | None,
) -> InboundMessage:
    """Read exact platform bindings and this Space's cards; never fetch a profile."""
    accounts = tuple(
        dict.fromkeys(item.target_user_id for item in message.mentions if item.target_user_id)
    )
    bindings = (
        {
            account: (person_id, nickname, person_name, root_id)
            for account, person_id, nickname, person_name, root_id in (
                await session.execute(
                    select(
                        IdentityBindingModel.external_account_id,
                        IdentityBindingModel.person_id,
                        IdentityBindingModel.display_name,
                        CanonicalPersonModel.display_name,
                        person_root_id(IdentityBindingModel.person_id),
                    )
                    .outerjoin(
                        CanonicalPersonModel,
                        CanonicalPersonModel.id == person_root_id(IdentityBindingModel.person_id),
                    )
                    .where(
                        IdentityBindingModel.platform == message.platform,
                        IdentityBindingModel.external_account_id.in_(accounts),
                        IdentityBindingModel.status == "active",
                    )
                )
            ).all()
        }
        if accounts
        else {}
    )
    presences: dict[str, str] = (
        {
            account: identity
            for account, identity in (
                await session.execute(
                    select(PresenceModel.external_account_id, PresenceModel.id).where(
                        PresenceModel.platform == message.platform,
                        PresenceModel.external_account_id.in_(accounts),
                        PresenceModel.enabled.is_(True),
                    )
                )
            ).all()
        }
        if accounts
        else {}
    )
    cards: dict[str, str] = {}
    if bindings and space_id is not None:
        for person_id, card in (
            await session.execute(
                select(
                    person_root_id(MembershipModel.canonical_person_id), MembershipModel.group_card
                )
                .where(
                    person_root_id(MembershipModel.canonical_person_id).in_(
                        tuple(values[3] for values in bindings.values())
                    ),
                    MembershipModel.canonical_space_id == space_id,
                )
                .order_by(MembershipModel.last_seen_at.desc(), MembershipModel.canonical_person_id)
            )
        ).all():
            cards.setdefault(person_id, card)
    resolved = []
    observed_names: dict[str, str] = {}
    for mention in message.mentions:
        if mention.kind == "all":
            resolved.append(mention)
            continue
        account = mention.target_user_id
        presence_id = presences.get(account or "")
        person_id, nickname, person_name, root_id = bindings.get(account or "", (None, "", "", ""))
        name = (
            mention.display_name
            or (message.sender.display_name if account == message.sender.user_id else "")
            or cards.get(root_id, "")
            or nickname
            or person_name
        )
        if not name and account and message.conversation_id:
            if account not in observed_names:
                previous = (
                    await session.execute(
                        select(ChatEventModel.sender_group_card, ChatEventModel.sender_nickname)
                        .join(PresenceModel, PresenceModel.id == ChatEventModel.ingress_presence_id)
                        .where(
                            PresenceModel.platform == message.platform,
                            ChatEventModel.sender_user_id == account,
                            ChatEventModel.canonical_conversation_id == message.conversation_id,
                            ChatEventModel.occurred_at <= message.received_at,
                            ChatEventModel.suppression_status == "keeper",
                        )
                        .order_by(ChatEventModel.occurred_at.desc(), ChatEventModel.id.desc())
                        .limit(1)
                    )
                ).first()
                observed_names[account] = (previous[0] or previous[1]) if previous else ""
            name = observed_names[account]
        name = name or ("Yuki" if presence_id is not None else "未识别成员")
        resolved.append(
            replace(
                mention,
                kind="yuki" if presence_id is not None else "member",
                display_name=sanitize_display_name(name),
                person_id=None if presence_id is not None else person_id,
                presence_id=presence_id,
            )
        )
    text, mentions = render_message_mentions(message.text, tuple(resolved))
    reply_text, reply_mentions = render_message_mentions(
        message.reply_text or "", message.reply_mentions
    )
    return replace(
        message,
        text=text,
        mentions=mentions,
        reply_text=reply_text if message.reply_text is not None else None,
        reply_mentions=reply_mentions,
        mentioned_person_ids=tuple(
            dict.fromkeys(item.person_id for item in mentions if item.person_id)
        )
        if mentions
        else message.mentioned_person_ids,
        mentioned_user_ids=tuple(
            dict.fromkeys(
                item.target_user_id
                for item in mentions
                if item.kind == "member" and item.target_user_id is not None
            )
        )
        if mentions
        else message.mentioned_user_ids,
        mentions_bot=any(item.kind == "yuki" for item in mentions)
        if mentions
        else message.mentions_bot,
    )
