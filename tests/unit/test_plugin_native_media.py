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

from yuki.automation.models import TurnOrigin
from yuki.capabilities.results import ToolExecutionResult
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import AttachmentKind, InboundMessage, MessageAttachment, SenderIdentity
from yuki.plugin_host.capability_adapter import PluginCapabilityAdapter
from yuki.plugin_host.db_models import PluginInstallationModel, PluginMediaArtifactModel
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.services.agent_tools import ToolRuntime
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.native_media import NativeMediaPreparer
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration
from yuki_plugin_sdk.results import PluginResult, ToolResult
from yuki_plugin_sdk.testing.fake_services import FakeMediaFacade


async def test_current_media_shares_native_platform_facts_without_byte_locators():
    preview = MessageAttachment(AttachmentKind.IMAGE, "preview", file="private-preview-id")
    sticker = MessageAttachment(
        AttachmentKind.FILE,
        "sticker",
        file="private-file-id",
        url="https://private.invalid",
        filename="sticker.tgs",
        is_sticker=True,
        sticker_format="animated",
        mime_type="application/x-tgsticker",
        width=64,
        height=64,
        preview=preview,
    )
    reply = MessageAttachment(
        AttachmentKind.IMAGE,
        "image",
        source="reply",
        sub_type="1",
        is_sticker=True,
        file="private-qq-id",
        emoji_id="native-qq-id",
        width=32,
        height=32,
    )
    inbound = InboundMessage(
        "1",
        "message",
        ScopeType.PRIVATE,
        SenderIdentity("1001"),
        "look",
        attachments=(sticker,),
        reply_attachments=(reply,),
    )
    context = HostPluginContext(
        plugin_id="media-view",
        approved_permissions=(PluginPermission.MEDIA_CURRENT_READ,),
        services=PluginFacadeServices(),
    )
    with context.bind(
        PluginInvocation(
            plugin_id="media-view",
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="1001",
            bot_user_id="8000",
            inbound=inbound,
        )
    ):
        current = await context.media.get_current()
    assert current[0]["is_sticker"] and current[0]["preview_available"]
    assert current[0]["sticker_format"] == "animated"
    assert current[0]["mime_type"] == "application/x-tgsticker"
    assert current[0]["filename"] == "sticker.tgs" and current[0]["width"] == 64
    assert current[1]["source"] == "reply" and current[1]["sub_type"] == "1"
    assert current[1]["is_sticker"] and not current[1]["preview_available"]
    assert "private-" not in json.dumps(current)
    fake = FakeMediaFacade()
    fake.current = [dict(item) for item in current]
    assert await fake.get_current() == current


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
                        plugin_api=PLUGIN_API_VERSION,
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
    runtime = ToolRuntime(inbound=inbound)
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
    result = await env.adapter.execute(env.name, "{}", env.runtime)
    assert isinstance(result, ToolExecutionResult)
    assert len(result.images) == 1
    image = result.images[0]
    assert image.plugin_media_handle == env.handle.handle_id
    assert image.plugin_approval_revision == "original-manifest"
    assert image.expires_at == env.handle.expires_at.isoformat()
    assert "data:image" not in json.dumps(
        result.model_payload()
    ) and "media_artifacts" not in json.dumps(result.model_payload())
    await env.adapter.validate_images(result.images, env.runtime)
    env.installation.approved_permissions = (PluginPermission.TOOL_REGISTER.value,)
    with pytest.raises(PluginPermissionError):
        await env.adapter.validate_images(result.images, env.runtime)
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_media_selection_requires_explicit_permission_without_losing_result(
    database, tmp_path
):
    env = await environment(database, tmp_path, permissions=(PluginPermission.TOOL_REGISTER,))
    result = await env.adapter.execute(env.name, "{}", env.runtime)
    payload = result.model_payload()
    assert payload["ok"] is True and payload["data"]["selected"] is True
    assert payload["data"]["media_error"] == "PluginPermissionError"
    assert payload["data"]["media_read"] is False and not result.images
    assert len(env.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "expire", "bytes"])
async def test_dispatch_hook_refuses_invalid_origin_despite_live_archive(
    database, tmp_path, change
):
    from yuki.services.main_agent_backend import MainAgentBackend

    env = await environment(database, tmp_path)
    result = await env.adapter.execute(env.name, "{}", env.runtime)
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
    "change", ["delete", "expire", "digest", "revision", "disabled", "tool", "closed"]
)
async def test_archive_copy_cannot_bypass_original_plugin_source(database, tmp_path, change):
    env = await environment(database, tmp_path)
    result = await env.adapter.execute(env.name, "{}", env.runtime)
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
    with pytest.raises(PluginPermissionError):
        await env.adapter.validate_images(images, env.runtime)
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
    result = await env.adapter.execute(env.name, "{}", env.runtime)
    assert not result.images
    assert result.model_payload()["data"]["media_error"] == "PluginPermissionError"
    assert result.model_payload()["data"]["media_read"] is False
    assert len(env.calls) == 1

    async def fake_json(context, handle):
        return ToolResult(data={"media_artifacts": [foreign_handle.model_dump(mode="json")]})

    env = await environment(database, tmp_path, handler=fake_json)
    assert not (await env.adapter.execute(env.name, "{}", env.runtime)).images


def test_empty_sdk_result_serialization_stays_compatible():
    assert "media_artifacts" not in PluginResult().model_dump(mode="json")


@pytest.mark.asyncio
async def test_scheduled_media_uses_real_plugin_approval_without_second_menu(database, tmp_path):
    env = await environment(database, tmp_path)
    from yuki.domain.tool_actor import ToolActor

    actor = ToolActor(
        user_id="10001",
        bot_user_id="99999",
        group_id=None,
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        instruction="select owned image",
        execution_id="scheduled-media-occurrence",
        conversation_id="scheduled-source-conversation",
    )
    source = replace(
        env.runtime,
        inbound=None,
        actor_context=actor,
        origin=actor.origin,
        execution_id=actor.execution_id,
        conversation_id=actor.conversation_id,
        scope_type=ScopeType.PRIVATE,
        external_target_id=actor.user_id,
        conversation_key="private:scheduled-source",
    )
    result = await env.adapter.execute(env.name, "{}", source)
    assert result.ok and result.images
    assert len(env.calls) == 1
