"""Stable speech cache keys and configured retention cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from qq_ai_bot.speech.models import SpeechGeneration, VoiceProfile, VoiceReference
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.repository import SpeechGenerationRepository
from qq_ai_bot.speech.text_normalizer import NORMALIZER_VERSION

GENIE_TTS_VERSION = "2.0.2"


def speech_cache_key(
    *,
    profile: VoiceProfile,
    reference: VoiceReference,
    normalized_text: str,
    split_sentence: bool,
    target_language: str,
    frontend_signature: str = "",
) -> str:
    payload = {
        "engine": GENIE_TTS_VERSION,
        "model_checksum": profile.model_checksum,
        "reference_checksum": reference.audio_checksum,
        "reference_language": reference.language,
        "target_language": target_language,
        "text": normalized_text,
        "split_sentence": split_sentence,
        "normalizer": NORMALIZER_VERSION,
        "frontend_signature": frontend_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SpeechCache:
    def __init__(
        self,
        *,
        repository: SpeechGenerationRepository,
        paths: SpeechPathPolicy,
    ) -> None:
        self._repository = repository
        self._paths = paths

    async def find(self, cache_key: str) -> SpeechGeneration | None:
        row = await self._repository.find_cache_hit(cache_key, now=datetime.now(UTC))
        if row is None or not row.output_relative_path:
            return None
        path = self._paths.resolve(row.output_relative_path)
        return row if path.is_file() else None

    async def cleanup(self, retention_hours: int | None) -> tuple[int, int]:
        if retention_hours is None:
            return 0, 0
        expired = await self._repository.expire_created_before(
            datetime.now(UTC) - timedelta(hours=retention_hours)
        )
        deleted = 0
        for row in expired:
            if not row.output_relative_path:
                continue
            path = self._paths.resolve(row.output_relative_path)
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                continue
            deleted += 1
        return len(expired), deleted
