"""Persistence for person speech preferences."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.services.canonical_owners import resolve_live_person_id
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
            person_id = await resolve_live_person_id(session, user_id)
            row = await session.get(PersonSpeechPreferenceModel, person_id)
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
            person_id = await resolve_live_person_id(session, user_id)
            row = await session.get(PersonSpeechPreferenceModel, person_id)
            if row is None:
                row = PersonSpeechPreferenceModel(
                    canonical_person_id=person_id,
                    mode=mode.value,
                    source_message_id=source_message_id[:128],
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(row)
            else:
                row.mode = mode.value
                row.source_message_id = source_message_id[:128]
                row.updated_at = timestamp
            await session.flush()
            return self._record(row, user_id=user_id)

    async def delete(self, user_id: str) -> bool:
        async with self._database.sessions() as session, session.begin():
            person_id = await resolve_live_person_id(session, user_id)
            row = await session.get(PersonSpeechPreferenceModel, person_id)
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
            user_id=user_id or row.canonical_person_id,
            mode=VoicePreferenceMode(row.mode),
            source_message_id=row.source_message_id,
            created_at=_aware_utc(row.created_at),
            updated_at=_aware_utc(row.updated_at),
        )


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
