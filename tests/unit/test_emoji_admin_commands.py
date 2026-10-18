"""The retired pool has no current command or SDK; original receipts remain readable."""

import pytest
from tests.conftest import make_settings

from yuki.admin.authority.command_types import CommandOperation, project_replayed_result
from yuki.admin.config_registry import ConfigRegistry
from yuki.admin.config_service import RuntimeConfigService
from yuki.services.commands.help import ADMIN_ACTIONS, ADMIN_HELP
from yuki_plugin_sdk.api import DEFAULT_FEATURES, PLUGIN_API_VERSION, is_api_compatible
from yuki_plugin_sdk.events import EventName
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing.fake_context import FakePluginContext


def test_retired_pool_is_absent_from_current_plugin_and_command_contract():
    assert PLUGIN_API_VERSION == "5.0"
    assert is_api_compatible("5.0") and not is_api_compatible("4.0")
    assert "emoji" not in ADMIN_ACTIONS and "/ai admin emoji" not in ADMIN_HELP
    assert not any(value.startswith("emoji.") for value in DEFAULT_FEATURES)
    assert not any(value.value.startswith("emoji.") for value in PluginPermission)
    assert not any(value.value.startswith("emoji.") for value in EventName)
    assert not hasattr(FakePluginContext("fixture"), "emoji")


async def test_runtime_has_no_retired_collection_or_worker_policy(database):
    settings = make_settings(database.url)
    snapshot = await RuntimeConfigService(settings=settings, database=database).snapshot()
    assert not hasattr(settings, "emoji") and not hasattr(snapshot, "emoji")
    assert not any(key.startswith("emoji.") for key in ConfigRegistry().keys)


@pytest.mark.parametrize("action", ["adopt", "unadopt", "import", "repair"])
def test_original_emoji_control_receipt_keeps_original_change(action):
    state = {
        "resource": "original-asset",
        "revision": 1,
        "status": "applied",
        "change": {"action": action, "adopted": action == "adopt"},
    }
    result = project_replayed_result(
        operation=CommandOperation.EMOJI_MUTATE,
        resource_id="original-asset",
        revision=1,
        audit_id="original-audit",
        raw_state=state,
        semantic_target_id="original-asset",
        material={"action": action},
    )
    assert result.success and result.resource_id == "original-asset"
    assert result.effective_state["change"] == state["change"]
