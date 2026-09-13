"""Permission-checked service Facade protocols exposed to a running plugin."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from yuki_plugin_sdk.events import EventEnvelope
from yuki_plugin_sdk.features import FeatureRegistry
from yuki_plugin_sdk.models import (
    BackgroundTargetGrantView,
    CurrentMessage,
    GeneratedSpeechHandle,
    JsonValue,
    MediaArtifactHandle,
    NotificationPublishReceipt,
    NotificationTarget,
    PublishNotificationRequest,
)
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.sessions import AgentSessionFacade


class MessageFacade(Protocol):
    async def get_current(self) -> CurrentMessage | None: ...

    async def get_reply(self) -> CurrentMessage | None: ...

    async def get_recent(self, limit: int = 20) -> tuple[CurrentMessage, ...]: ...

    async def search_history(self, query: str, limit: int = 20) -> tuple[CurrentMessage, ...]: ...

    async def send_text(self, text: str) -> PluginResult: ...

    async def send_private(self, user_id: str, text: str) -> PluginResult: ...

    async def send_group(self, group_id: str, text: str) -> PluginResult: ...

    async def send_image(
        self, *, target_type: str, target_id: str, media_reference: str
    ) -> PluginResult: ...


class PeopleFacade(Protocol):
    async def get_current(self) -> Mapping[str, JsonValue] | None: ...

    async def get(self, user_id: str) -> Mapping[str, JsonValue] | None: ...

    async def list_aliases(self, user_id: str) -> tuple[str, ...]: ...

    async def add_alias(self, user_id: str, alias: str) -> PluginResult: ...


class GroupFacade(Protocol):
    async def get_current(self) -> Mapping[str, JsonValue] | None: ...

    async def get(self, group_id: str) -> Mapping[str, JsonValue] | None: ...

    async def list_members(
        self, group_id: str, limit: int = 100
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def get_settings(self, group_id: str) -> Mapping[str, JsonValue]: ...

    async def set_setting(self, group_id: str, key: str, value: JsonValue) -> PluginResult: ...


class MemoryFacade(Protocol):
    async def list_person(
        self, user_id: str, limit: int = 20
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def list_group(
        self, group_id: str, limit: int = 20
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def search(
        self,
        query: str,
        *,
        scope_type: str,
        subject_id: str,
        limit: int = 20,
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def add(
        self,
        *,
        scope_type: str,
        subject_id: str,
        content: str,
        source_type: str,
        confidence: float,
        source_event_ids: tuple[str, ...] = (),
    ) -> PluginResult: ...

    async def update(
        self,
        memory_id: str,
        *,
        content: str,
        confidence: float | None = None,
    ) -> PluginResult: ...

    async def delete(self, memory_id: str) -> PluginResult: ...


class RelationshipFacade(Protocol):
    async def get_current(self) -> Mapping[str, JsonValue] | None: ...

    async def get(self, user_id: str) -> Mapping[str, JsonValue] | None: ...

    async def list_events(
        self, user_id: str, limit: int = 20
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def adjust(
        self,
        user_id: str,
        *,
        affection_delta: int = 0,
        trust_delta: int = 0,
        reason: str,
    ) -> PluginResult: ...


class LLMFacade(Protocol):
    """Yuki Main Agent generation in a real Host-bound Conversation.

    Does not send the returned text. Missing source/target identity is an error;
    use target-bound notifications for background wakeups, or agent_sessions for
    explicitly independent computation. Context profiles do not grant history reads.
    """

    async def generate(
        self, instruction: str, *, max_characters: int = 2_000
    ) -> str | PluginResult: ...

    async def generate_with_context(
        self,
        instruction: str,
        *,
        context_profile: str,
        max_characters: int = 2_000,
    ) -> str | PluginResult: ...


class AgentFacade(Protocol):
    """Yuki Main Agent with the plugin's approved capability intersection.

    Requires the same real invocation as LLMFacade; this is no longer an isolated
    random-key session. Main Agent declarations do not expand execution authority.
    """

    async def run(
        self,
        instruction: str,
        *,
        allowed_capabilities: tuple[str, ...] = (),
        max_tool_calls: int | None = None,
        max_model_requests: int | None = None,
    ) -> PluginResult: ...


class WebFacade(Protocol):
    async def search(self, query: str) -> PluginResult: ...

    async def read(self, url: str, question: str = "") -> PluginResult: ...


class MCPFacade(Protocol):
    async def status(self) -> Mapping[str, JsonValue]: ...

    async def list_servers(self) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def search_tools(self, query: str) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def call(
        self,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> PluginResult: ...


class HttpFacade(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        auth_secret: str | None = None,
    ) -> PluginResult: ...


class VisionFacade(Protocol):
    async def get_current_observation(self) -> Mapping[str, JsonValue] | None: ...

    async def analyze_current_media(self, question: str = "") -> PluginResult: ...


class MediaFacade(Protocol):
    async def get_current(self) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def create_artifact(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        ttl_seconds: int = 86_400,
    ) -> MediaArtifactHandle: ...


class NotificationFacade(Protocol):
    async def publish(
        self,
        request: PublishNotificationRequest,
    ) -> NotificationPublishReceipt: ...

    async def grant_target(
        self,
        target: NotificationTarget,
        *,
        bot_user_id: str,
    ) -> BackgroundTargetGrantView: ...

    async def revoke_target(self, target: NotificationTarget) -> bool: ...

    async def list_grants(self) -> tuple[BackgroundTargetGrantView, ...]: ...

    async def status(self) -> Mapping[str, int]: ...


class EmojiFacade(Protocol):
    async def list(
        self, status: str | None = None, limit: int = 30
    ) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def get(self, emoji_id: str) -> Mapping[str, JsonValue] | None: ...

    async def search(self, query: str, limit: int = 20) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def collect_current(self) -> PluginResult: ...

    async def select(
        self,
        *,
        goal: str,
        emotion: str = "",
        mode: str = "optional",
        placement: str = "after_text",
    ) -> PluginResult: ...

    async def queue_reply_effect(
        self,
        *,
        goal: str,
        emotion: str = "",
        mode: str = "optional",
        placement: str = "after_text",
    ) -> PluginResult: ...

    async def adopt(
        self, emoji_id: str, *, scope_type: str = "global", scope_id: str = ""
    ) -> PluginResult: ...

    async def reject(self, emoji_id: str) -> PluginResult: ...

    async def ban(self, emoji_id: str) -> PluginResult: ...


class SpeechFacade(Protocol):
    async def status(self) -> Mapping[str, JsonValue]: ...

    async def list_profiles(self) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def get_profile(self, profile_id: str) -> Mapping[str, JsonValue] | None: ...

    async def list_styles(self, profile_id: str) -> tuple[str, ...]: ...

    async def synthesize(
        self,
        text: str,
        *,
        profile_id: str = "",
        style_hint: str = "",
    ) -> GeneratedSpeechHandle: ...

    async def queue_reply_voice(
        self,
        *,
        profile_id: str = "",
        style_hint: str = "",
        mode: str = "optional",
    ) -> PluginResult: ...

    async def send_private(self, user_id: str, handle: GeneratedSpeechHandle) -> PluginResult: ...

    async def send_group(self, group_id: str, handle: GeneratedSpeechHandle) -> PluginResult: ...


class AutomationFacade(Protocol):
    async def list_current_owner(self) -> tuple[Mapping[str, JsonValue], ...]: ...

    async def create_from_template(
        self, template: str, parameters: Mapping[str, JsonValue]
    ) -> PluginResult: ...

    async def pause(self, task_id: str) -> PluginResult: ...

    async def resume(self, task_id: str) -> PluginResult: ...

    async def cancel(self, task_id: str) -> PluginResult: ...


class ConfigFacade(Protocol):
    async def get(
        self, key: str, *, scope_type: str = "global", scope_id: str = ""
    ) -> JsonValue: ...

    async def set(
        self,
        key: str,
        value: JsonValue,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> None: ...


class SecretsFacade(Protocol):
    def configured(self, name: str) -> bool: ...

    def get(self, name: str) -> str: ...


class StorageFacade(Protocol):
    async def get(self, namespace: str, key: str) -> JsonValue: ...

    async def set(self, namespace: str, key: str, value: JsonValue) -> None: ...

    async def delete(self, namespace: str, key: str) -> bool: ...

    async def list(self, namespace: str) -> Mapping[str, JsonValue]: ...

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: JsonValue,
        value: JsonValue,
    ) -> bool: ...


ManagedRunner = Callable[[], Awaitable[None]]


class SchedulerFacade(Protocol):
    def create_task(self, name: str, runner: ManagedRunner) -> str: ...

    async def cancel(self, task_id: str) -> bool: ...

    async def sleep_until_stopped(self) -> None: ...


class OneBotFacade(Protocol):
    async def send_music_card(self, *, provider: str, resource_id: str) -> PluginResult: ...

    async def send_custom_music_card(
        self,
        *,
        url: str,
        image: str,
        title: str,
        singer: str = "",
        content: str = "",
    ) -> PluginResult: ...

    async def send_private(self, user_id: str, text: str) -> PluginResult: ...

    async def send_group(self, group_id: str, text: str) -> PluginResult: ...

    async def call_read_action(
        self, action: str, params: Mapping[str, JsonValue]
    ) -> PluginResult: ...

    async def call_mutating_action(
        self, action: str, params: Mapping[str, JsonValue]
    ) -> PluginResult: ...


class PluginEventPublisher(Protocol):
    async def publish(self, event: EventEnvelope) -> None: ...


class PluginContext(Protocol):
    """No property returns Settings, Container, DB sessions, Bot, or raw events."""

    @property
    def plugin_id(self) -> str: ...

    @property
    def logger(self) -> logging.Logger: ...

    @property
    def current(self) -> CurrentMessage | None: ...

    @property
    def features(self) -> FeatureRegistry: ...

    @property
    def messages(self) -> MessageFacade: ...

    @property
    def people(self) -> PeopleFacade: ...

    @property
    def groups(self) -> GroupFacade: ...

    @property
    def memory(self) -> MemoryFacade: ...

    @property
    def relationship(self) -> RelationshipFacade: ...

    @property
    def llm(self) -> LLMFacade: ...

    @property
    def agent(self) -> AgentFacade: ...

    @property
    def agent_sessions(self) -> AgentSessionFacade: ...

    @property
    def web(self) -> WebFacade: ...

    @property
    def mcp(self) -> MCPFacade: ...

    @property
    def http(self) -> HttpFacade: ...

    @property
    def vision(self) -> VisionFacade: ...

    @property
    def media(self) -> MediaFacade: ...

    @property
    def notifications(self) -> NotificationFacade: ...

    @property
    def emoji(self) -> EmojiFacade: ...

    @property
    def speech(self) -> SpeechFacade: ...

    @property
    def automation(self) -> AutomationFacade: ...

    @property
    def config(self) -> ConfigFacade: ...

    @property
    def secrets(self) -> SecretsFacade: ...

    @property
    def storage(self) -> StorageFacade: ...

    @property
    def scheduler(self) -> SchedulerFacade: ...

    @property
    def onebot(self) -> OneBotFacade: ...

    @property
    def events(self) -> PluginEventPublisher: ...
