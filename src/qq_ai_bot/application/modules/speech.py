"""Local speech application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.settings_domains import SpeechSettings
from qq_ai_bot.speech.admin import SpeechAdminService
from qq_ai_bot.speech.cache import SpeechCache
from qq_ai_bot.speech.delivery import VoiceDeliveryService
from qq_ai_bot.speech.genie_client import GenieWorkerClient
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
from qq_ai_bot.speech.preference_service import VoicePreferenceService
from qq_ai_bot.speech.profiles import VoiceProfileService
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository
from qq_ai_bot.speech.service import GenieTTSProvider, SpeechService


@dataclass(frozen=True, slots=True)
class SpeechBundle:
    preferences: VoicePreferenceService
    paths: SpeechPathPolicy
    cache: SpeechCache
    worker: GenieWorkerClient
    provider: GenieTTSProvider
    service: SpeechService
    profiles: VoiceProfileService
    delivery: VoiceDeliveryService
    admin: SpeechAdminService


class SpeechModule:
    def __init__(
        self,
        *,
        settings: SpeechSettings,
        preference_repository: VoicePreferenceRepository,
        profile_repository: VoiceProfileRepository,
        generation_repository: SpeechGenerationRepository,
        turns: ConversationTurnCoordinator,
        runtime_config: RuntimeConfigService,
        lifecycle: LifecycleRegistry,
        bot_display_name: str = "Yuki",
        bot_voice_name: str = "ゆき",
    ) -> None:
        self._settings = settings
        self._preference_repository = preference_repository
        self._profile_repository = profile_repository
        self._generation_repository = generation_repository
        self._turns = turns
        self._runtime_config = runtime_config
        self._lifecycle = lifecycle
        self._bot_display_name = bot_display_name
        self._bot_voice_name = bot_voice_name

    def build(self) -> SpeechBundle:
        settings = self._settings
        preferences = VoicePreferenceService(self._preference_repository)
        paths = SpeechPathPolicy(settings.speech_root)
        cache = SpeechCache(repository=self._generation_repository, paths=paths)
        worker = GenieWorkerClient(
            settings.speech_socket_path,
            request_timeout_seconds=settings.speech_worker_request_timeout_seconds,
        )
        provider = GenieTTSProvider(
            client=worker,
            profiles=self._profile_repository,
            generations=self._generation_repository,
            cache=cache,
            paths=paths,
        )
        service = SpeechService(
            provider=provider,
            generations=self._generation_repository,
            cache=cache,
            paths=paths,
            profiles=self._profile_repository,
            turns=self._turns,
        )
        profiles = VoiceProfileService(
            repository=self._profile_repository,
            paths=paths,
            loader=worker if settings.speech_enabled else None,
        )
        delivery = VoiceDeliveryService(
            service,
            bot_display_name=self._bot_display_name,
            bot_voice_name=self._bot_voice_name,
        )
        admin = SpeechAdminService(
            speech=service,
            profiles=profiles,
            runtime_config=self._runtime_config,
            worker=worker,
            bot_display_name=self._bot_display_name,
        )
        self._lifecycle.register("speech", close=service.close, health=service.health)
        return SpeechBundle(
            preferences,
            paths,
            cache,
            worker,
            provider,
            service,
            profiles,
            delivery,
            admin,
        )
