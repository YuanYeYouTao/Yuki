"""Ready-to-use PluginContext for plugin tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from yuki_plugin_sdk.api import DEFAULT_FEATURES
from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.features import FeatureRegistry
from yuki_plugin_sdk.models import CurrentMessage
from yuki_plugin_sdk.testing.fake_services import (
    FakeAgentFacade,
    FakeAgentSessionFacade,
    FakeAutomationFacade,
    FakeConfigFacade,
    FakeEmojiFacade,
    FakeEventBus,
    FakeGroupFacade,
    FakeHttpFacade,
    FakeLLMFacade,
    FakeMediaFacade,
    FakeMemoryFacade,
    FakeMessageFacade,
    FakeNotificationFacade,
    FakeOneBotFacade,
    FakePeopleFacade,
    FakeRelationshipFacade,
    FakeScheduler,
    FakeSecretsFacade,
    FakeStorage,
    FakeVisionFacade,
    FakeWebFacade,
)


@dataclass(slots=True)
class FakePluginContext:
    plugin_id: str
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("yuki.plugin.test"))
    features: FeatureRegistry = field(default_factory=lambda: FeatureRegistry(DEFAULT_FEATURES))
    messages: FakeMessageFacade = field(default_factory=FakeMessageFacade)
    people: FakePeopleFacade = field(default_factory=FakePeopleFacade)
    groups: FakeGroupFacade = field(default_factory=FakeGroupFacade)
    memory: FakeMemoryFacade = field(default_factory=FakeMemoryFacade)
    relationship: FakeRelationshipFacade = field(default_factory=FakeRelationshipFacade)
    llm: FakeLLMFacade = field(default_factory=FakeLLMFacade)
    agent: FakeAgentFacade = field(default_factory=FakeAgentFacade)
    agent_sessions: FakeAgentSessionFacade = field(default_factory=FakeAgentSessionFacade)
    web: FakeWebFacade = field(default_factory=FakeWebFacade)
    http: FakeHttpFacade = field(default_factory=FakeHttpFacade)
    vision: FakeVisionFacade = field(default_factory=FakeVisionFacade)
    media: FakeMediaFacade = field(default_factory=FakeMediaFacade)
    notifications: FakeNotificationFacade = field(default_factory=FakeNotificationFacade)
    automation: FakeAutomationFacade = field(default_factory=FakeAutomationFacade)
    config: FakeConfigFacade = field(default_factory=FakeConfigFacade)
    emoji: FakeEmojiFacade = field(default_factory=FakeEmojiFacade)
    secrets: FakeSecretsFacade = field(default_factory=FakeSecretsFacade)
    storage: FakeStorage = field(default_factory=FakeStorage)
    scheduler: FakeScheduler = field(default_factory=FakeScheduler)
    onebot: FakeOneBotFacade = field(default_factory=FakeOneBotFacade)
    events: FakeEventBus = field(default_factory=FakeEventBus)

    @property
    def current(self) -> CurrentMessage | None:
        return self.messages.current


def _assert_plugin_context_contract(context: FakePluginContext) -> PluginContext:
    """Keep the testing fake structurally checked against the public Protocol."""

    return context
