"""Persistence boundary for voice profiles, references, and generated speech."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.shadows import conversation_id_for_event
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.speech.db_models import (
    SpeechGenerationModel,
    SpeechVoiceProfileModel,
    SpeechVoiceReferenceModel,
)
from qq_ai_bot.speech.models import (
    SpeechEngineModelVersion,
    SpeechGeneration,
    SpeechGenerationStatus,
    VoiceProfile,
    VoiceReference,
)


class VoiceProfileRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_profiles(self, *, enabled_only: bool = False) -> tuple[VoiceProfile, ...]:
        statement = select(SpeechVoiceProfileModel).order_by(
            SpeechVoiceProfileModel.is_default.desc(), SpeechVoiceProfileModel.profile_id
        )
        if enabled_only:
            statement = statement.where(SpeechVoiceProfileModel.enabled.is_(True))
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            references = await self._references_for(session, [row.profile_id for row in rows])
            return tuple(self._profile(row, references.get(row.profile_id, ())) for row in rows)

    async def get_profile(
        self, profile_id: str, *, session: AsyncSession | None = None
    ) -> VoiceProfile | None:
        from qq_ai_bot.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=False) as active:
            row = await active.get(SpeechVoiceProfileModel, profile_id)
            if row is None:
                return None
            references = await self._references_for(active, [profile_id])
            return self._profile(row, references.get(profile_id, ()))

    async def get_default(self) -> VoiceProfile | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(SpeechVoiceProfileModel).where(
                    SpeechVoiceProfileModel.enabled.is_(True),
                    SpeechVoiceProfileModel.is_default.is_(True),
                )
            )
            if row is None:
                return None
            references = await self._references_for(session, [row.profile_id])
            return self._profile(row, references.get(row.profile_id, ()))

    async def save_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        provider: str,
        engine_model_version: str,
        language: str,
        supported_languages: tuple[str, ...],
        model_relative_path: str,
        model_checksum: str,
        default_style: str,
        enabled: bool,
        source: str,
        source_note: str,
        license_note: str,
        manifest_hash: str,
        references: tuple[dict[str, object], ...],
        now: datetime | None = None,
    ) -> VoiceProfile:
        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(SpeechVoiceProfileModel, profile_id)
            if row is None:
                row = SpeechVoiceProfileModel(
                    profile_id=profile_id,
                    is_default=False,
                    created_at=timestamp,
                )
                session.add(row)
            row.display_name = display_name
            row.provider = provider
            row.engine_model_version = engine_model_version
            row.language = language
            row.supported_languages_json = json.dumps(
                supported_languages, ensure_ascii=False, separators=(",", ":")
            )
            row.model_relative_path = model_relative_path
            row.model_checksum = model_checksum
            row.default_style = default_style
            row.enabled = enabled
            if not enabled:
                row.is_default = False
            row.source = source
            row.source_note = source_note
            row.license_note = license_note
            row.manifest_hash = manifest_hash
            row.updated_at = timestamp
            existing = {
                item.reference_key: item
                for item in (
                    await session.scalars(
                        select(SpeechVoiceReferenceModel).where(
                            SpeechVoiceReferenceModel.profile_id == profile_id
                        )
                    )
                ).all()
            }
            incoming: set[str] = set()
            for values in references:
                key = str(values["reference_key"])
                incoming.add(key)
                reference = existing.get(key)
                if reference is None:
                    reference = SpeechVoiceReferenceModel(
                        profile_id=profile_id,
                        reference_key=key,
                        created_at=timestamp,
                    )
                    session.add(reference)
                reference.style = str(values["style"])
                reference.aliases_json = json.dumps(values["aliases"], ensure_ascii=False)
                reference.audio_relative_path = str(values["audio_relative_path"])
                reference.audio_checksum = str(values["audio_checksum"])
                reference.transcript = str(values["transcript"])
                reference.language = str(values["language"])
                reference.enabled = bool(values["enabled"])
                priority = values["priority"]
                if not isinstance(priority, int):
                    raise TypeError("voice reference priority must be an integer")
                reference.priority = priority
                reference.updated_at = timestamp
            stale = set(existing) - incoming
            if stale:
                await session.execute(
                    delete(SpeechVoiceReferenceModel).where(
                        SpeechVoiceReferenceModel.profile_id == profile_id,
                        SpeechVoiceReferenceModel.reference_key.in_(stale),
                    )
                )
            await session.flush()
        result = await self.get_profile(profile_id)
        if result is None:
            raise RuntimeError("voice profile was not persisted")
        return result

    async def activate(self, profile_id: str) -> VoiceProfile:
        timestamp = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(SpeechVoiceProfileModel, profile_id)
            if row is None:
                raise LookupError("voice profile not found")
            if not row.enabled:
                raise ValueError("disabled voice profile cannot be activated")
            await session.execute(update(SpeechVoiceProfileModel).values(is_default=False))
            row.is_default = True
            row.updated_at = timestamp
        profile = await self.get_profile(profile_id)
        if profile is None:
            raise RuntimeError("activated voice profile disappeared")
        return profile

    async def set_enabled(
        self,
        profile_id: str,
        *,
        enabled: bool,
        session: AsyncSession | None = None,
    ) -> VoiceProfile:
        from qq_ai_bot.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=True) as active:
            row = await active.get(SpeechVoiceProfileModel, profile_id)
            if row is None:
                raise LookupError("voice profile not found")
            row.enabled = enabled
            if not enabled:
                row.is_default = False
            row.updated_at = datetime.now(UTC)
            await active.flush()
            references = await self._references_for(active, [profile_id])
            profile = self._profile(row, references.get(profile_id, ()))
        if profile is None:
            raise RuntimeError("updated voice profile disappeared")
        return profile

    async def set_reference_enabled(
        self, profile_id: str, reference_key: str, *, enabled: bool
    ) -> VoiceReference:
        async with self._database.sessions() as session, session.begin():
            row = await session.scalar(
                select(SpeechVoiceReferenceModel).where(
                    SpeechVoiceReferenceModel.profile_id == profile_id,
                    SpeechVoiceReferenceModel.reference_key == reference_key,
                )
            )
            if row is None:
                raise LookupError("voice reference not found")
            row.enabled = enabled
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return self._reference(row)

    async def set_default_style(self, profile_id: str, style: str) -> VoiceProfile:
        async with self._database.sessions() as session, session.begin():
            matches = (
                await session.scalars(
                    select(SpeechVoiceReferenceModel).where(
                        SpeechVoiceReferenceModel.profile_id == profile_id,
                        SpeechVoiceReferenceModel.style == style,
                        SpeechVoiceReferenceModel.enabled.is_(True),
                    )
                )
            ).all()
            if len(matches) != 1:
                raise ValueError("default style must identify one enabled reference")
            profile = await session.get(SpeechVoiceProfileModel, profile_id)
            if profile is None:
                raise LookupError("voice profile not found")
            profile.default_style = style
            profile.updated_at = datetime.now(UTC)
        result = await self.get_profile(profile_id)
        if result is None:
            raise RuntimeError("voice profile disappeared")
        return result

    async def _references_for(
        self, session: AsyncSession, profile_ids: list[str]
    ) -> dict[str, tuple[VoiceReference, ...]]:
        if not profile_ids:
            return {}
        rows = (
            await session.scalars(
                select(SpeechVoiceReferenceModel)
                .where(SpeechVoiceReferenceModel.profile_id.in_(profile_ids))
                .order_by(
                    SpeechVoiceReferenceModel.profile_id,
                    SpeechVoiceReferenceModel.priority.desc(),
                    SpeechVoiceReferenceModel.reference_key,
                )
            )
        ).all()
        grouped: dict[str, list[VoiceReference]] = {}
        for row in rows:
            grouped.setdefault(row.profile_id, []).append(self._reference(row))
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _profile(
        row: SpeechVoiceProfileModel, references: tuple[VoiceReference, ...]
    ) -> VoiceProfile:
        supported_languages = json.loads(row.supported_languages_json)
        if not isinstance(supported_languages, list) or not all(
            isinstance(item, str) for item in supported_languages
        ):
            raise ValueError("stored supported speech languages are invalid")
        return VoiceProfile(
            profile_id=row.profile_id,
            display_name=row.display_name,
            provider="genie",
            engine_model_version=SpeechEngineModelVersion(row.engine_model_version),
            language=row.language,
            supported_languages=tuple(supported_languages) or (row.language,),
            model_relative_path=row.model_relative_path,
            model_checksum=row.model_checksum,
            default_style=row.default_style,
            enabled=row.enabled,
            is_default=row.is_default,
            source=row.source,
            source_note=row.source_note,
            license_note=row.license_note,
            manifest_hash=row.manifest_hash,
            references=references,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _reference(row: SpeechVoiceReferenceModel) -> VoiceReference:
        aliases = json.loads(row.aliases_json)
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise ValueError("stored voice reference aliases are invalid")
        return VoiceReference(
            id=row.id,
            profile_id=row.profile_id,
            reference_key=row.reference_key,
            style=row.style,
            aliases=tuple(aliases),
            audio_relative_path=row.audio_relative_path,
            audio_checksum=row.audio_checksum,
            transcript=row.transcript,
            language=row.language,
            enabled=row.enabled,
            priority=row.priority,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SpeechGenerationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        *,
        request_id: str,
        conversation_key_hash: str,
        trigger_event_id: int | None,
        profile_id: str,
        reference_id: int,
        engine_version: str,
        target_language: str,
        text_hash: str,
        normalized_text_hash: str,
        character_count: int,
        cache_key: str,
        expires_at: datetime | None,
    ) -> SpeechGeneration:
        row = SpeechGenerationModel(
            request_id=request_id,
            conversation_key_hash=conversation_key_hash,
            trigger_event_id=trigger_event_id,
            profile_id=profile_id,
            reference_id=reference_id,
            engine_version=engine_version,
            target_language=target_language,
            text_hash=text_hash,
            normalized_text_hash=normalized_text_hash,
            character_count=character_count,
            cache_key=cache_key,
            status=SpeechGenerationStatus.QUEUED.value,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        async with self._database.sessions() as session, session.begin():
            session.add(row)
            await session.flush()
            proven = await conversation_id_for_event(session, trigger_event_id)
            if proven is not None and row.canonical_conversation_id is None:
                row.canonical_conversation_id = proven
            return self._generation(row)

    async def get(self, generation_id: int) -> SpeechGeneration | None:
        async with self._database.sessions() as session:
            row = await session.get(SpeechGenerationModel, generation_id)
            return self._generation(row) if row is not None else None

    async def find_cache_hit(self, cache_key: str, *, now: datetime) -> SpeechGeneration | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(SpeechGenerationModel)
                .join(
                    SpeechVoiceProfileModel,
                    SpeechVoiceProfileModel.profile_id == SpeechGenerationModel.profile_id,
                )
                .outerjoin(
                    SpeechVoiceReferenceModel,
                    SpeechVoiceReferenceModel.id == SpeechGenerationModel.reference_id,
                )
                .where(
                    SpeechGenerationModel.cache_key == cache_key,
                    SpeechGenerationModel.status.in_(
                        (SpeechGenerationStatus.SUCCEEDED.value, SpeechGenerationStatus.SENT.value)
                    ),
                    or_(
                        SpeechGenerationModel.expires_at.is_(None),
                        SpeechGenerationModel.expires_at > now,
                    ),
                    SpeechVoiceProfileModel.enabled.is_(True),
                    SpeechVoiceReferenceModel.enabled.is_(True),
                )
                .order_by(SpeechGenerationModel.created_at.desc())
            )
            return self._generation(row) if row is not None else None

    async def set_generating(self, generation_id: int) -> SpeechGeneration:
        return await self._set_status(generation_id, SpeechGenerationStatus.GENERATING)

    async def complete(
        self,
        generation_id: int,
        *,
        output_relative_path: str,
        sample_rate: int,
        channels: int,
        duration_milliseconds: int,
    ) -> SpeechGeneration:
        async with self._database.sessions() as session, session.begin():
            row = await session.get(SpeechGenerationModel, generation_id)
            if row is None:
                raise LookupError("speech generation not found")
            row.output_relative_path = output_relative_path
            row.sample_rate = sample_rate
            row.channels = channels
            row.duration_milliseconds = duration_milliseconds
            row.status = SpeechGenerationStatus.SUCCEEDED.value
            row.error_category = None
            await session.flush()
            return self._generation(row)

    async def mark_failed(self, generation_id: int, error_category: str) -> SpeechGeneration:
        return await self._set_status(
            generation_id, SpeechGenerationStatus.FAILED, error_category=error_category
        )

    async def mark_cancelled(self, generation_id: int) -> SpeechGeneration:
        return await self._set_status(generation_id, SpeechGenerationStatus.CANCELLED)

    async def mark_sent(self, generation_id: int) -> SpeechGeneration:
        return await self._set_status(generation_id, SpeechGenerationStatus.SENT)

    async def expire_before(self, now: datetime) -> tuple[SpeechGeneration, ...]:
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(SpeechGenerationModel).where(
                        SpeechGenerationModel.expires_at.is_not(None),
                        SpeechGenerationModel.expires_at <= now,
                        SpeechGenerationModel.status.in_(
                            (
                                SpeechGenerationStatus.SUCCEEDED.value,
                                SpeechGenerationStatus.SENT.value,
                            )
                        ),
                    )
                )
            ).all()
            for row in rows:
                row.status = SpeechGenerationStatus.EXPIRED.value
            await session.flush()
            return tuple(self._generation(row) for row in rows)

    async def expire_created_before(self, cutoff: datetime) -> tuple[SpeechGeneration, ...]:
        """Expire successful cache rows using the current HOT retention policy."""

        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(SpeechGenerationModel).where(
                        SpeechGenerationModel.created_at <= cutoff,
                        SpeechGenerationModel.status.in_(
                            (
                                SpeechGenerationStatus.SUCCEEDED.value,
                                SpeechGenerationStatus.SENT.value,
                            )
                        ),
                    )
                )
            ).all()
            for row in rows:
                row.status = SpeechGenerationStatus.EXPIRED.value
            await session.flush()
            return tuple(self._generation(row) for row in rows)

    async def queue_depth(self) -> int:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(SpeechGenerationModel.id).where(
                        SpeechGenerationModel.status.in_(
                            (
                                SpeechGenerationStatus.QUEUED.value,
                                SpeechGenerationStatus.GENERATING.value,
                            )
                        )
                    )
                )
            ).all()
            return len(rows)

    async def _set_status(
        self,
        generation_id: int,
        status: SpeechGenerationStatus,
        *,
        error_category: str | None = None,
    ) -> SpeechGeneration:
        async with self._database.sessions() as session, session.begin():
            row = await session.get(SpeechGenerationModel, generation_id)
            if row is None:
                raise LookupError("speech generation not found")
            row.status = status.value
            row.error_category = error_category
            await session.flush()
            return self._generation(row)

    @staticmethod
    def _generation(row: SpeechGenerationModel) -> SpeechGeneration:
        return SpeechGeneration(
            id=row.id,
            request_id=row.request_id,
            conversation_key_hash=row.conversation_key_hash,
            trigger_event_id=row.trigger_event_id,
            profile_id=row.profile_id,
            reference_id=row.reference_id,
            engine_version=row.engine_version,
            target_language=row.target_language,
            text_hash=row.text_hash,
            normalized_text_hash=row.normalized_text_hash,
            character_count=row.character_count,
            cache_key=row.cache_key,
            output_relative_path=row.output_relative_path,
            output_format=row.output_format,
            sample_rate=row.sample_rate,
            channels=row.channels,
            duration_milliseconds=row.duration_milliseconds,
            status=SpeechGenerationStatus(row.status),
            error_category=row.error_category,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
