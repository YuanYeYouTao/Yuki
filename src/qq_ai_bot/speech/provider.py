"""Provider-neutral speech synthesis contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from qq_ai_bot.services.turn_coordinator import TurnToken


@dataclass(frozen=True, slots=True)
class SpeechSynthesisRequest:
    request_id: str
    profile_id: str
    style_hint: str
    text: str
    split_sentence: bool
    conversation_key: str
    trigger_event_id: int | None
    turn_token: TurnToken | None
    language_hint: str = "auto"
    canonical_conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    generation_id: int
    profile_id: str
    reference_key: str
    target_language: str
    relative_path: str
    format: str
    sample_rate: int
    channels: int
    duration_milliseconds: int
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class SpeechProviderHealth:
    available: bool
    connected: bool
    ready: bool
    busy: bool
    loaded_profile_id: str | None = None
    detail: str = ""
    japanese_frontend_available: bool | None = None
    japanese_frontend_version: str | None = None
    japanese_frontend_signature: str | None = None


class TTSProvider(Protocol):
    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> SynthesizedSpeech: ...

    async def health(self) -> SpeechProviderHealth: ...

    async def close(self) -> None: ...
