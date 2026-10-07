"""Explicit plugin selection keeps pixels private and original authority revocable."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import delete, update

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
from qq_ai_bot.plugin_host.db_models import PluginInstallationModel, PluginMediaArtifactModel
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.native_media import NativeMediaPreparer
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration
from yuki_plugin_sdk.results import PluginResult, ToolResult


class Arguments(StrictModel):
    pass


def pixels():
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(stream, format="PNG")
    return stream.getvalue()


async def environment(database, tmp_path, *, permissions=None, handler=None):
    permissions = permissions or (
        PluginPermission.TOOL_REGISTER,
        PluginPermission.MEDIA_ARTIFACT_CREATE,
    )
    store = PluginMediaArtifactStore(database, tmp_path / "plugin-images")
    async with database.sessions() as session, session.begin():
        for plugin_id in ("example.plugin", "other.plugin"):
            if await session.get(PluginInstallationModel, plugin_id) is None:
                session.add(
                    PluginInstallationModel(
                        plugin_id=plugin_id,
                        name=plugin_id,
                        version="1.0",
                        plugin_api="3.2",
                        yuki_requires="*",
                        manifest_hash="original-manifest",
                        entrypoint="test:plugin",
                        status="running",
                        enabled=True,
                        discovered_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
    handle = await store.create(
        plugin_id="example.plugin",
        data=pixels(),
        content_type="image/png",
        filename="selected.png",
        ttl_seconds=3600,
        storage_mb=5,
    )
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=permissions,
        services=PluginFacadeServices(
            approval_revision="original-manifest",
            media_artifacts=store,
            native_media=NativeMediaPreparer(ImagePreprocessor()),
        ),
    )
    calls = []

    async def select(arguments):
        calls.append(True)
        if handler is not None:
            return await handler(context, handle)
        return ToolResult(data={"selected": True}, media_artifacts=(handle,))

    registry = ExtensionRegistry()
    registry.registrar("example.plugin", permissions).register_tool(
        ToolRegistration(
            ToolMetadata(name="select", description="select owned image"),
            Arguments,
            ToolResult,
            select,
        )
    )
    installation = SimpleNamespace(
        enabled=True,
        manifest_hash="original-manifest",
        approved_permissions=tuple(permission.value for permission in permissions),
    )
    adapter = PluginCapabilityAdapter(
        registry=registry,
        installations=SimpleNamespace(get=AsyncMock(return_value=installation)),
        invocation_scope=context.invocation_scope,
    )
    inbound = InboundMessage(
        message_id="1",
        bot_user_id="99999",
        sender=SenderIdentity("10001"),
        text="pick",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
    )
    runtime = ToolRuntime(inbound=inbound, gateway=None, allow_generic_onebot=False)
    name = registry.list(kind=ExtensionKind.TOOL)[0].model_name
    return SimpleNamespace(
        store=store,
        handle=handle,
        context=context,
        adapter=adapter,
        runtime=runtime,
        name=name,
        installation=installation,
        calls=calls,
    )


@pytest.mark.asyncio
async def test_explicit_owned_selection_is_private_and_rechecked(database, tmp_path):
    env = await environment(database, tmp_path)
    result = await env.adapter.execute(env.name, "{}", env.runtime, web_was_used=False)
    assert isinstance(result, ToolExecutionResult)
    assert len(result.images) == 1
    image = result.images[0]
    assert image.plugin_media_handle == env.handle.handle_id
    assert image.plugin_approval_revision == "original-manifest"
    assert image.expires_at == env.handle.expires_at.isoformat()
    assert "data:image" not in json.dumps(
        result.model_payload()
    ) and "media_artifacts" not in json.dumps(result.model_payload())
    await env.adapter.validate_images(result.images, env.runtime, web_was_used=False)
    env.installation.approved_permissions = (PluginPermission.TOOL_REGISTER.value,)
    with pytest.raises(PluginPermissionError):
        await env.adapter.validate_images(result.images, env.runtime, web_was_used=False)
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_media_selection_requires_explicit_permission_without_losing_result(
    database, tmp_path
):
    env = await environment(database, tmp_path, permissions=(PluginPermission.TOOL_REGISTER,))
    result = await env.adapter.execute(env.name, "{}", env.runtime, web_was_used=False)
    payload = result.model_payload()
    assert payload["ok"] is True and payload["data"]["selected"] is True
    assert payload["data"]["media_error"] == "PluginPermissionError"
    assert payload["data"]["media_read"] is False and not result.images
    assert len(env.calls) == 1
    assert not hasattr(env.context, "mcp")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "expire", "bytes"])
async def test_dispatch_hook_refuses_invalid_origin_despite_live_archive(
    database, tmp_path, change
):
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    env = await environment(database, tmp_path)
    result = await env.adapter.execute(env.name, "{}", env.runtime, web_was_used=False)
    original = await env.store.resolve(plugin_id="example.plugin", handle_id=env.handle.handle_id)
    archived = tuple(replace(image, tool_handle="retained-private-copy") for image in result.images)
    archive_validator = AsyncMock()
    backend = MainAgentBackend(
        SimpleNamespace(
            _plugin_tools=env.adapter,
            _tool_artifacts=SimpleNamespace(validate_media=archive_validator),
        ),
        env.runtime,
    )
    if change == "bytes":
        original.local_path.write_bytes(b"x" * original.byte_size)
    else:
        async with database.sessions() as session, session.begin():
            statement = (
                delete(PluginMediaArtifactModel)
                if change == "delete"
                else update(PluginMediaArtifactModel)
            )
            statement = statement.where(PluginMediaArtifactModel.handle_id == env.handle.handle_id)
            if change == "expire":
                statement = statement.values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            await session.execute(statement)
    with pytest.raises(PluginPermissionError):
        await backend.validate_images(archived, SimpleNamespace(work_control=None))
    archive_validator.assert_not_called()
    assert len(env.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["delete", "expire", "digest", "revision", "disabled", "tool", "closed", "origin"]
)
async def test_archive_copy_cannot_bypass_original_plugin_source(database, tmp_path, change):
    env = await environment(database, tmp_path)
    result = await env.adapter.execute(env.name, "{}", env.runtime, web_was_used=False)
    # An external-tool archive handle is only a private copy, not a new read grant.
    images = tuple(replace(image, tool_handle="private-archive-copy") for image in result.images)
    if change in {"delete", "expire", "digest"}:
        async with database.sessions() as session, session.begin():
            statement = (
                delete(PluginMediaArtifactModel)
                if change == "delete"
                else update(PluginMediaArtifactModel)
            )
            statement = statement.where(PluginMediaArtifactModel.handle_id == env.handle.handle_id)
            if change == "expire":
                statement = statement.values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            elif change == "digest":
                statement = statement.values(sha256="0" * 64)
            await session.execute(statement)
    elif change == "revision":
        env.installation.manifest_hash = "replaced-plugin"
    elif change == "disabled":
        env.installation.enabled = False
    elif change == "tool":
        images = tuple(replace(image, plugin_tool_name="another-action") for image in images)
    elif change == "closed":
        env.runtime = replace(env.runtime, tools_closed=True)
    else:
        env.runtime = replace(env.runtime, origin=TurnOrigin.SYSTEM_TASK, actor_context=None)
    with pytest.raises(PluginPermissionError):
        await env.adapter.validate_images(images, env.runtime, web_was_used=False)
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_foreign_owned_handle_and_fake_json_never_grant_read(database, tmp_path):
    async def foreign(context, handle):
        return ToolResult(media_artifacts=(handle.model_copy(update={"handle_id": "foreign"}),))

    env = await environment(database, tmp_path, handler=foreign)
    foreign_handle = await env.store.create(
        plugin_id="other.plugin",
        data=pixels(),
        content_type="image/png",
        filename="foreign.png",
        ttl_seconds=3600,
        storage_mb=5,
    )

    async def select_foreign(context, handle):
        return ToolResult(media_artifacts=(foreign_handle,))

    env = await environment(database, tmp_path, handler=select_foreign)
    result = await env.adapter.execute(env.name, "{}", env.runtime, web_was_used=False)
    assert not result.images
    assert result.model_payload()["data"]["media_error"] == "PluginPermissionError"
    assert result.model_payload()["data"]["media_read"] is False
    assert len(env.calls) == 1

    async def fake_json(context, handle):
        return ToolResult(data={"media_artifacts": [foreign_handle.model_dump(mode="json")]})

    env = await environment(database, tmp_path, handler=fake_json)
    assert not (await env.adapter.execute(env.name, "{}", env.runtime, web_was_used=False)).images


def test_empty_sdk_result_serialization_stays_compatible():
    assert "media_artifacts" not in PluginResult().model_dump(mode="json")


@pytest.mark.asyncio
async def test_plugin_prefix_delegation_cannot_authorize_another_selected_action(
    database, tmp_path
):
    env = await environment(database, tmp_path)
    from qq_ai_bot.identity.canonical_repository import ensure_person

    async with database.immediate_session() as session:
        person_id = await ensure_person(session, "10001")
    inbound = replace(env.runtime.inbound, person_id=person_id)
    action = "plugin.example.plugin.tool.select"
    invocation = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id="10001",
        bot_user_id="99999",
        inbound=inbound,
        actor_person_id=person_id,
        delegated_authority=SimpleNamespace(
            creator_user_id="10001", canonical_creator_person_id=person_id
        ),
        allowed_capabilities=frozenset({"plugin.example.plugin.tool.other"}),
    )
    with env.context.bind(invocation), pytest.raises(PluginPermissionError, match="not delegated"):
        env.context._authorize_tool_media(action)
    with env.context.bind(replace(invocation, allowed_capabilities=frozenset({action}))):
        env.context._authorize_tool_media(action)
