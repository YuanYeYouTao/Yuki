"""A wait in the original plugin scope cannot retain stale dispatch approval."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from yuki_plugin_sdk.models import RetryPolicy, RiskClass
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration


@pytest.mark.parametrize("change", ["disabled", "handler_replaced"])
async def test_revocation_during_scope_wait_refuses_handler(tmp_path, change):
    class Arguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    external = tmp_path / "plugin-external.log"

    async def handler(arguments):
        external.write_text("committed")
        return {}

    registration = ToolRegistration(
        ToolMetadata(
            name="write",
            description="write",
            risk=RiskClass.MUTATE,
            retry_policy=RetryPolicy.TRANSIENT_ONCE,
        ),
        Arguments,
        Arguments,
        handler,
    )
    item = SimpleNamespace(kind=ExtensionKind.TOOL, plugin_id="offline", registration=registration)
    current = item
    installation = SimpleNamespace(enabled=True)

    async def get(_plugin_id):
        return installation

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        nonlocal current
        if change == "disabled":
            installation.enabled = False
        else:
            current = SimpleNamespace(**vars(item))
        yield

    adapter = PluginCapabilityAdapter(
        registry=SimpleNamespace(resolve_model_name=lambda _name: current),
        installations=SimpleNamespace(get=get),
        invocation_scope=scope,
    )
    runtime = ToolRuntime(inbound=None, gateway=None, allow_generic_onebot=False)
    result = (await adapter.execute("write", "{}", runtime, web_was_used=False)).model_payload()
    assert result["error_code"] == "plugin_tool_denied"
    assert not external.exists()


async def test_real_installation_manifest_revision_revokes_queued_tool(database, tmp_path):
    class Arguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    external = tmp_path / "approved-plugin-external.log"

    async def handler(arguments):
        external.write_text("committed")
        return {}

    installations = PluginInstallationRepository(database)
    discovered = dict(
        plugin_id="offline",
        name="offline",
        version="1.0",
        plugin_api="1",
        yuki_requires=">=3",
        entrypoint="plugin:Plugin",
        requested_permissions=("tool.register",),
    )
    await installations.upsert_discovered(**discovered, manifest_hash="a" * 64)
    await installations.approve("offline", permissions=("tool.register",))
    await installations.set_enabled("offline", enabled=True)
    registry = ExtensionRegistry()
    registry.registrar("offline", (PluginPermission.TOOL_REGISTER,)).register_tool(
        ToolRegistration(
            ToolMetadata(name="write", description="write", risk=RiskClass.MUTATE),
            Arguments,
            Arguments,
            handler,
        )
    )
    item = registry.list(kind=ExtensionKind.TOOL)[0]

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        # Discovery of a changed approved manifest atomically clears approval.
        await installations.upsert_discovered(**discovered, manifest_hash="b" * 64)
        yield

    adapter = PluginCapabilityAdapter(
        registry=registry, installations=installations, invocation_scope=scope
    )
    runtime = ToolRuntime(inbound=None, gateway=None, allow_generic_onebot=False)
    result = (
        await adapter.execute(item.model_name, "{}", runtime, web_was_used=False)
    ).model_payload()
    assert result["error_code"] == "plugin_tool_denied" and not external.exists()
    assert registry.resolve_model_name(item.model_name) is item
    installation = await installations.get("offline")
    assert installation is not None and not installation.enabled
    assert installation.approved_permissions == ()
