"""Reads and writes of the QQ official message facts; see the model module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from yuki.gateway.providers.qq_official_db_models import QQOfficialMessageModel
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel


async def record_message(
    database: Database,
    *,
    presence_id: str,
    message_id: str,
    chat_kind: str,
    chat_id: str,
    direction: str,
    ref_idx: str | None = None,
    reply_to: str | None = None,
    msg_seq: int | None = None,
    created_at: datetime | None = None,
) -> None:
    """Keep the first fact of one platform message; redelivery changes nothing."""
    async with database.immediate_session() as session:
        await session.execute(
            insert(QQOfficialMessageModel)
            .values(
                presence_id=presence_id,
                message_id=message_id,
                chat_kind=chat_kind,
                chat_id=chat_id,
                direction=direction,
                ref_idx=ref_idx,
                reply_to=reply_to,
                msg_seq=msg_seq,
                created_at=created_at or datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )


async def next_msg_seq(database: Database, presence_id: str, reply_to: str) -> int:
    async with database.read_session() as session:
        last = await session.scalar(
            select(func.max(QQOfficialMessageModel.msg_seq)).where(
                QQOfficialMessageModel.presence_id == presence_id,
                QQOfficialMessageModel.reply_to == reply_to,
            )
        )
    return (last or 0) + 1


async def latest_inbound(
    database: Database, presence_id: str, chat_kind: str, chat_id: str
) -> str | None:
    async with database.read_session() as session:
        return cast(
            str | None,
            await session.scalar(
                select(QQOfficialMessageModel.message_id)
                .where(
                    QQOfficialMessageModel.presence_id == presence_id,
                    QQOfficialMessageModel.chat_kind == chat_kind,
                    QQOfficialMessageModel.chat_id == chat_id,
                    QQOfficialMessageModel.direction == "inbound",
                )
                .order_by(QQOfficialMessageModel.created_at.desc())
                .limit(1)
            ),
        )


async def ref_idx_for(database: Database, presence_id: str, message_id: str) -> str | None:
    async with database.read_session() as session:
        row = await session.get(QQOfficialMessageModel, (presence_id, message_id))
    return row.ref_idx if row is not None else None


async def message_for_ref(database: Database, presence_id: str, ref_idx: str) -> str | None:
    async with database.read_session() as session:
        return cast(
            str | None,
            await session.scalar(
                select(QQOfficialMessageModel.message_id).where(
                    QQOfficialMessageModel.presence_id == presence_id,
                    QQOfficialMessageModel.ref_idx == ref_idx,
                )
            ),
        )


async def is_outbound(database: Database, presence_id: str, message_id: str) -> bool:
    async with database.read_session() as session:
        row = await session.get(QQOfficialMessageModel, (presence_id, message_id))
    return row is not None and row.direction == "outbound"


async def trigger_message(database: Database, presence_id: str, event_id: int) -> str | None:
    """The platform msg_id of an internal inbound event received by this Presence."""
    async with database.read_session() as session:
        event = await session.get(ChatEventModel, event_id)
    if event is None or event.direction != "inbound" or event.ingress_presence_id != presence_id:
        return None
    return event.native_message_id
