"""Maintain the one derived history index at the original event write boundary."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.persistence.history_search import namespace_terms, prepare_terms

CREATE_HISTORY_FTS = """CREATE VIRTUAL TABLE chat_history_fts USING fts5(
    tokens, content='', contentless_delete=1, detail=none, tokenize='ascii'
)"""
DELETE_HISTORY_TRIGGER = """CREATE TRIGGER chat_history_fts_ad
AFTER DELETE ON chat_events BEGIN
    DELETE FROM chat_history_fts WHERE rowid=OLD.id;
END"""


async def replace_event(
    session: AsyncSession,
    event_id: int,
    conversation_id: str | None,
    prepared_terms: tuple[str, ...],
    *,
    author_person_id: str | None = None,
    author_kind: str | None = None,
) -> None:
    """Replace a validated source's derived row in its existing transaction."""
    if conversation_id is not None and prepared_terms:
        await session.execute(
            text("INSERT OR REPLACE INTO chat_history_fts(rowid,tokens) VALUES(:id,:tokens)"),
            {
                "id": event_id,
                "tokens": namespace_terms(
                    conversation_id,
                    prepared_terms,
                    author_person_id=author_person_id,
                    author_kind=author_kind,
                ),
            },
        )
    else:
        await session.execute(
            text("DELETE FROM chat_history_fts WHERE rowid=:id"), {"id": event_id}
        )


def rebuild_history_search(connection: Connection) -> None:
    """Fill the unpublished 0144 stage with writers stopped by the operator.

    A restart rebuilds only this derived stage. Reads and text conversion finish
    before each short import transaction; no build state or shadow table exists.
    """
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        connection.exec_driver_sql(
            "INSERT INTO chat_history_fts(chat_history_fts) VALUES('delete-all')"
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    after = 0
    while True:
        rows = connection.exec_driver_sql(
            "SELECT id,canonical_conversation_id,content,audio_transcript,"
            "author_person_id,author_kind "
            "FROM chat_events WHERE id>? AND canonical_conversation_id IS NOT NULL "
            "AND (suppression_status IS NULL OR suppression_status='keeper') "
            "AND event_kind IN ('message','external_event') ORDER BY id LIMIT 256",
            (after,),
        ).fetchall()
        connection.commit()
        if not rows:
            return
        prepared = [
            (
                event_id,
                namespace_terms(
                    conversation_id, terms, author_person_id=person_id, author_kind=author_kind
                ),
            )
            for event_id, conversation_id, content, audio, person_id, author_kind in rows
            if (terms := prepare_terms(content, audio))
        ]
        if prepared:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                connection.exec_driver_sql(
                    "INSERT INTO chat_history_fts(rowid,tokens) VALUES(?,?)", prepared
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        after = rows[-1][0]
