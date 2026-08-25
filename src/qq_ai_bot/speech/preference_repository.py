"""Persistence for person speech preferences."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from qq_ai_bot.identity.canonical_projections import resolve_canonical_speech_preference
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.speech.db_models import PersonSpeechPreferenceModel
from qq_ai_bot.speech.models import VoicePreferenceMode


@dataclass(frozen=True, slots=True)
class PersonSpeechPreference:
    user_id: str
    mode: VoicePreferenceMode
    source_message_id: str
    created_at: datetime
    updated_at: datetime


class VoicePreferenceRepository:
    """Read and atomically replace the one canonical mode for a person."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, user_id: str) -> PersonSpeechPreference | None:
        async with self._database.sessions() as session:
            row = await resolve_canonical_speech_preference(
                session,
                user_id,
                create=False,
            )
            return self._record(row, user_id=user_id) if row is not None else None

    async def set(
        self,
        user_id: str,
        mode: VoicePreferenceMode,
        *,
        source_message_id: str,
        now: datetime | None = None,
    ) -> PersonSpeechPreference:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            row = await resolve_canonical_speech_preference(
                session,
                user_id,
                create=True,
                mode=mode.value,
                source_message_id=source_message_id,
                now=timestamp,
            )
            if row is None:
                raise RuntimeError("speech preference was not persisted")
            return self._record(row, user_id=user_id)

    async def delete(self, user_id: str) -> bool:
        async with self._database.sessions() as session, session.begin():
            row = await resolve_canonical_speech_preference(
                session,
                user_id,
                create=False,
            )
            if row is None:
                return False
            await session.delete(row)
            return True

    @staticmethod
    def _record(
        row: PersonSpeechPreferenceModel,
        *,
        user_id: str | None = None,
    ) -> PersonSpeechPreference:
        return PersonSpeechPreference(
            user_id=user_id or row.user_id,
            mode=VoicePreferenceMode(row.mode),
            source_message_id=row.source_message_id,
            created_at=_aware_utc(row.created_at),
            updated_at=_aware_utc(row.updated_at),
        )


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
