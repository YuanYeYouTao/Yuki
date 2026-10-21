"""Freeze real mention targets and locally observed names before event publication."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.messages import InboundMessage, render_message_mentions, sanitize_display_name
from yuki.identity.db_models import (
    AccountModel,
    AccountSpaceProfileModel,
    SpaceBindingModel,
)
from yuki.identity.ownership_interval import assignments_at


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
    account_rows = (
        list(
            await session.scalars(
                select(AccountModel).where(
                    AccountModel.platform == message.platform,
                    AccountModel.external_account_id.in_(accounts),
                )
            )
        )
        if accounts
        else []
    )
    stages = await assignments_at(
        session, tuple(account.id for account in account_rows), message.occurred_at
    )
    identities = {
        account.external_account_id: (account, stages.get(account.id)) for account in account_rows
    }
    cards: dict[str, str] = {}
    if identities and message.group_id is not None:
        cards = {
            key: value
            for key, value in (
                await session.execute(
                    select(
                        AccountSpaceProfileModel.account_id, AccountSpaceProfileModel.display_name
                    )
                    .join(
                        SpaceBindingModel,
                        SpaceBindingModel.id == AccountSpaceProfileModel.space_binding_id,
                    )
                    .where(
                        SpaceBindingModel.platform == message.platform,
                        SpaceBindingModel.external_space_id == message.group_id,
                        AccountSpaceProfileModel.account_id.in_(
                            [account.id for account, _ in identities.values()]
                        ),
                    )
                )
            ).all()
        }

    resolved = []
    for mention in message.mentions:
        if mention.kind == "all":
            resolved.append(mention)
            continue
        identity = identities.get(mention.target_user_id or "")
        account, assignment = identity if identity is not None else (None, None)
        is_yuki = assignment is not None and assignment.kind == "yuki"
        current_names_apply = account is not None and (
            message.occurred_at is None or (assignment is not None and assignment.status != "ended")
        )
        name = (
            mention.display_name
            or (
                message.sender.display_name
                if mention.target_user_id == message.sender.user_id
                else ""
            )
            or (cards.get(account.id, "") if current_names_apply and account else "")
            or (account.display_name if current_names_apply and account else "")
            or ("Agent" if is_yuki else "未识别成员")
        )
        resolved.append(
            replace(
                mention,
                kind="yuki" if is_yuki else mention.kind,
                display_name=sanitize_display_name(name),
                account_id=account.id if account else None,
                assignment_id=assignment.id if assignment else None,
                person_id=assignment.person_id if assignment else None,
                presence_id=assignment.id if assignment and is_yuki else None,
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
