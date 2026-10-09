"""Public testing helpers for Yuki plugins."""

from yuki_plugin_sdk.testing.contract import PluginContractReport, run_plugin_contract_tests
from yuki_plugin_sdk.testing.fake_context import FakePluginContext
from yuki_plugin_sdk.testing.fake_services import (
    FakeAgentFacade,
    FakeAgentSessionFacade,
    FakeAutomationFacade,
    FakeClock,
    FakeConfigFacade,
    FakeEventBus,
    FakeGroupFacade,
    FakeHttpFacade,
    FakeMediaFacade,
    FakeMemoryFacade,
    FakeMessageFacade,
    FakeOneBotFacade,
    FakePeopleFacade,
    FakeScheduler,
    FakeSecretsFacade,
    FakeStorage,
    FakeVisionFacade,
    FakeWebFacade,
)

__all__ = [
    "FakeAgentFacade",
    "FakeAgentSessionFacade",
    "FakeAutomationFacade",
    "FakeClock",
    "FakeConfigFacade",
    "FakeEventBus",
    "FakeGroupFacade",
    "FakeHttpFacade",
    "FakeMediaFacade",
    "FakeMemoryFacade",
    "FakeMessageFacade",
    "FakeOneBotFacade",
    "FakePeopleFacade",
    "FakePluginContext",
    "FakeScheduler",
    "FakeSecretsFacade",
    "FakeStorage",
    "FakeVisionFacade",
    "FakeWebFacade",
    "PluginContractReport",
    "run_plugin_contract_tests",
]
