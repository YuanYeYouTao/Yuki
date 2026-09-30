"""Repositories for visual-analysis and QQ emoji description caches."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    EmojiDescriptionModel,
    MediaAnalysisModel,
)
from qq_ai_bot.persistence.repository_records import (
    EmojiDescriptionRecord,
    MediaAnalysisRecord,
)


class MediaAnalysisRepository:
    """Persist and reuse expiring structured visual observations."""

    _MODES = frozenset({"general", "meme", "ocr", "question"})

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find_cached(
        self,
        *,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> MediaAnalysisRecord | None:
        """Find one unexpired exact cache entry for a prepared media payload."""

        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(MediaAnalysisModel).where(
                    MediaAnalysisModel.content_hash == content_hash,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.question_hash == (question_hash or ""),
                    MediaAnalysisModel.provider == provider,
                    MediaAnalysisModel.model == model,
                    MediaAnalysisModel.prompt_version == prompt_version,
                    MediaAnalysisModel.expires_at > timestamp,
                )
            )
        return self._record(row) if row is not None else None

    async def find_latest_for_content(
        self,
        *,
        content_hash: str,
        analysis_mode: str,
        prompt_version_suffix: str,
        now: datetime | None = None,
    ) -> MediaAnalysisRecord | None:
        """Reuse a current structured observation across compatible media consumers."""

        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(MediaAnalysisModel)
                .where(
                    MediaAnalysisModel.content_hash == content_hash,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.prompt_version.endswith(prompt_version_suffix),
                    MediaAnalysisModel.expires_at > timestamp,
                )
                .order_by(MediaAnalysisModel.created_at.desc())
                .limit(1)
            )
        return self._record(row) if row is not None else None

    async def find_for_event(
        self,
        source_event_id: int,
        segment_index: int,
        *,
        analysis_mode: str,
        question_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> MediaAnalysisRecord | None:
        """Find an unexpired observation attached to one exact ledger segment."""

        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(MediaAnalysisModel).where(
                    MediaAnalysisModel.source_event_id == source_event_id,
                    MediaAnalysisModel.segment_index == segment_index,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.question_hash == question_hash,
                    MediaAnalysisModel.provider == provider,
                    MediaAnalysisModel.model == model,
                    MediaAnalysisModel.prompt_version == prompt_version,
                    MediaAnalysisModel.expires_at > timestamp,
                )
            )
        return self._record(row) if row is not None else None

    async def save(
        self,
        *,
        source_event_id: int | None,
        segment_index: int,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        observation_json: str | dict[str, Any],
        expires_at: datetime,
        created_at: datetime | None = None,
    ) -> MediaAnalysisRecord:
        """Upsert a cache value while preserving its original event association."""

        self._validate_key(
            content_hash=content_hash,
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            segment_index=segment_index,
        )
        normalized_question_hash = question_hash or ""
        serialized = self._serialize_observation(observation_json)
        timestamp = created_at or datetime.now(UTC)
        values = {
            "source_event_id": source_event_id,
            "segment_index": segment_index,
            "content_hash": content_hash,
            "analysis_mode": analysis_mode,
            "question_hash": normalized_question_hash,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "observation_json": serialized,
            "created_at": timestamp,
            "expires_at": expires_at,
        }
        statement = insert(MediaAnalysisModel).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                MediaAnalysisModel.content_hash,
                MediaAnalysisModel.analysis_mode,
                MediaAnalysisModel.question_hash,
                MediaAnalysisModel.model,
                MediaAnalysisModel.prompt_version,
            ],
            set_={
                # A refresh may update the provider and observation, but a cache hit
                # never changes the source event that owns cascade deletion.
                "provider": provider,
                "observation_json": serialized,
                "created_at": timestamp,
                "expires_at": expires_at,
            },
        )
        async with self._database.sessions() as session, session.begin():
            await session.execute(statement)
            row = await session.scalar(
                select(MediaAnalysisModel).where(
                    MediaAnalysisModel.content_hash == content_hash,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.question_hash == normalized_question_hash,
                    MediaAnalysisModel.model == model,
                    MediaAnalysisModel.prompt_version == prompt_version,
                )
            )
            if row is None:  # pragma: no cover - guarded by the insert above
                raise RuntimeError("media analysis upsert did not return a row")
            return self._record(row)

    async def save_analysis(self, **values: Any) -> MediaAnalysisRecord:
        """Compatibility spelling for service-layer integrations."""

        return await self.save(**values)

    async def associate_event(
        self,
        analysis_id: int,
        *,
        source_event_id: int,
        segment_index: int,
    ) -> bool:
        """Attach an unowned cache row once; never move it between ledger events."""

        if segment_index < 0:
            raise ValueError("segment_index must be non-negative")
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MediaAnalysisModel)
                .where(
                    MediaAnalysisModel.id == analysis_id,
                    MediaAnalysisModel.source_event_id.is_(None),
                )
                .values(
                    source_event_id=source_event_id,
                    segment_index=segment_index,
                )
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def cleanup_expired(self, *, now: datetime | None = None, limit: int = 128) -> int:
        """Delete cache rows whose expiry has been reached."""

        cutoff = now or datetime.now(UTC)
        if not 1 <= limit <= 128:
            raise ValueError("cleanup page must be between 1 and 128")
        async with self._database.sessions() as session:
            ids = tuple(
                await session.scalars(
                    select(MediaAnalysisModel.id)
                    .where(MediaAnalysisModel.expires_at <= cutoff)
                    .order_by(MediaAnalysisModel.expires_at, MediaAnalysisModel.id)
                    .limit(limit)
                )
            )
        if not ids:
            return 0
        async with self._database.immediate_session() as session:
            result = await session.execute(
                delete(MediaAnalysisModel).where(
                    MediaAnalysisModel.id.in_(ids), MediaAnalysisModel.expires_at <= cutoff
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)

    @classmethod
    def _validate_key(
        cls,
        *,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        segment_index: int,
    ) -> None:
        if len(content_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest")
        try:
            int(content_hash, 16)
        except ValueError as exc:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest") from exc
        if analysis_mode not in cls._MODES:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")
        if analysis_mode == "question" and not question_hash:
            raise ValueError("question analysis requires question_hash")
        if question_hash and len(question_hash) > 64:
            raise ValueError("question_hash must not exceed 64 characters")
        if segment_index < 0:
            raise ValueError("segment_index must be non-negative")
        if not provider or len(provider) > 32:
            raise ValueError("provider must contain at most 32 characters")
        if not model or len(model) > 128:
            raise ValueError("model must contain at most 128 characters")
        if not prompt_version or len(prompt_version) > 64:
            raise ValueError("prompt_version must contain at most 64 characters")

    @staticmethod
    def _serialize_observation(value: str | dict[str, Any]) -> str:
        return _serialize_visual_observation(value)

    @staticmethod
    def _record(row: MediaAnalysisModel) -> MediaAnalysisRecord:
        return MediaAnalysisRecord(
            id=row.id,
            source_event_id=row.source_event_id,
            segment_index=row.segment_index,
            content_hash=row.content_hash,
            analysis_mode=row.analysis_mode,
            question_hash=row.question_hash,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            observation_json=row.observation_json,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )


class EmojiDescriptionRepository:
    """Persist visual observations behind durable, exact emoji lookup keys."""

    _MODES = frozenset({"general", "meme", "ocr", "question"})

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find_first(
        self,
        emoji_keys: tuple[str, ...],
        *,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> EmojiDescriptionRecord | None:
        """Return the first exact key match and atomically record its reuse."""

        keys = self._validated_keys(emoji_keys)
        self._validate_lookup(
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if not keys:
            return None
        timestamp = now or datetime.now(UTC)
        normalized_question_hash = question_hash or ""
        async with self._database.sessions() as session, session.begin():
            for key in keys:
                row = await session.scalar(
                    select(EmojiDescriptionModel).where(
                        EmojiDescriptionModel.emoji_key == key,
                        EmojiDescriptionModel.analysis_mode == analysis_mode,
                        EmojiDescriptionModel.question_hash == normalized_question_hash,
                        EmojiDescriptionModel.provider == provider,
                        EmojiDescriptionModel.model == model,
                        EmojiDescriptionModel.prompt_version == prompt_version,
                    )
                )
                if row is None:
                    continue
                row.hit_count += 1
                row.last_used_at = timestamp
                await session.flush()
                return self._record(row)
        return None

    async def save_many(
        self,
        emoji_keys: tuple[str, ...],
        *,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        observation_json: str | dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[EmojiDescriptionRecord, ...]:
        """Upsert one safe observation for every equivalent emoji identity key."""

        keys = self._validated_keys(emoji_keys)
        self._validate_lookup(
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if not keys:
            return ()
        serialized = _serialize_visual_observation(observation_json)
        parsed = json.loads(serialized)
        description_value = parsed.get("overall_description", "")
        description = description_value if isinstance(description_value, str) else ""
        description = " ".join(description.split())[:2000]
        timestamp = now or datetime.now(UTC)
        normalized_question_hash = question_hash or ""
        records: list[EmojiDescriptionRecord] = []
        async with self._database.sessions() as session, session.begin():
            for key in keys:
                values = {
                    "emoji_key": key,
                    "analysis_mode": analysis_mode,
                    "question_hash": normalized_question_hash,
                    "provider": provider,
                    "model": model,
                    "prompt_version": prompt_version,
                    "description": description,
                    "observation_json": serialized,
                    "hit_count": 0,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "last_used_at": timestamp,
                }
                statement = insert(EmojiDescriptionModel).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        EmojiDescriptionModel.emoji_key,
                        EmojiDescriptionModel.analysis_mode,
                        EmojiDescriptionModel.question_hash,
                        EmojiDescriptionModel.model,
                        EmojiDescriptionModel.prompt_version,
                    ],
                    set_={
                        "provider": provider,
                        "description": description,
                        "observation_json": serialized,
                        "updated_at": timestamp,
                        "last_used_at": timestamp,
                    },
                )
                await session.execute(statement)
                row = await session.scalar(
                    select(EmojiDescriptionModel).where(
                        EmojiDescriptionModel.emoji_key == key,
                        EmojiDescriptionModel.analysis_mode == analysis_mode,
                        EmojiDescriptionModel.question_hash == normalized_question_hash,
                        EmojiDescriptionModel.model == model,
                        EmojiDescriptionModel.prompt_version == prompt_version,
                    )
                )
                if row is None:  # pragma: no cover - guarded by the upsert above
                    raise RuntimeError("emoji description upsert did not return a row")
                records.append(self._record(row))
        return tuple(records)

    @classmethod
    def _validated_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        keys = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 255 for value in keys):
            raise ValueError("emoji_key must not exceed 255 characters")
        if any(not value.startswith(("package:", "emoji:", "file:", "content:")) for value in keys):
            raise ValueError("unsupported emoji_key namespace")
        return keys

    @classmethod
    def _validate_lookup(
        cls,
        *,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
    ) -> None:
        if analysis_mode not in cls._MODES:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")
        if analysis_mode == "question" and not question_hash:
            raise ValueError("question analysis requires question_hash")
        if question_hash and len(question_hash) > 64:
            raise ValueError("question_hash must not exceed 64 characters")
        if not provider or len(provider) > 32:
            raise ValueError("provider must contain at most 32 characters")
        if not model or len(model) > 128:
            raise ValueError("model must contain at most 128 characters")
        if not prompt_version or len(prompt_version) > 64:
            raise ValueError("prompt_version must contain at most 64 characters")

    @staticmethod
    def _record(row: EmojiDescriptionModel) -> EmojiDescriptionRecord:
        return EmojiDescriptionRecord(
            id=row.id,
            emoji_key=row.emoji_key,
            analysis_mode=row.analysis_mode,
            question_hash=row.question_hash,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            description=row.description,
            observation_json=row.observation_json,
            hit_count=row.hit_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_used_at=row.last_used_at,
        )


def _serialize_visual_observation(value: str | dict[str, Any]) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value

    def contains_embedded_media(item: Any) -> bool:
        if isinstance(item, str):
            lowered = item.lstrip().lower()
            return lowered.startswith(("data:image/", "base64://"))
        if isinstance(item, dict):
            return any(contains_embedded_media(child) for child in item.values())
        if isinstance(item, list | tuple):
            return any(contains_embedded_media(child) for child in item)
        return False

    if contains_embedded_media(parsed):
        raise ValueError("observation_json must not contain image or Base64 payloads")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
