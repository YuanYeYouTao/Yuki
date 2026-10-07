"""Authorized tool pixels stay outside public receipts and use the primary model."""

import base64
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.capabilities.media import PreparedMediaData
from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    ScopeType,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.persistence.models import ChatEventModel, ToolArtifactModel
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.native_media import NativeMediaPreparer
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.tool_results.access import ArtifactAccess, access_from_runtime
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


def _pixels():
    stream = io.BytesIO()
    Image.new("RGB", (48, 32), "blue").save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def _result():
    pixels = NativeMediaPreparer(ImagePreprocessor()).prepare_image(
        base64.b64decode(_pixels()), source="tool"
    )
    return ToolExecutionResult(
        ok=True,
        data={"summary": "screenshot result"},
        images=pixels,
        provider_id="plugin.browser",
        tool_name="screenshot",
    )


@pytest.mark.asyncio
async def test_render_and_image_reread_preserve_pixels_without_recursive_archive(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "media", retention_seconds=60)
    budgeter = ToolResultBudgeter(max_characters=2000, artifacts=store, artifact_access=access)
    result = _result()
    rendered = await budgeter.render(result)
    handle = json.loads(rendered.text)["media_artifact_handle"]
    assert rendered.text.images[0].tool_handle == handle
    assert "base64" not in rendered.text
    prepared = await store.read(handle, operation="image", access=access)
    reread = await budgeter.render(
        ToolExecutionResult(
            ok=True,
            data=prepared,
            images=prepared.images,
            provider_id="artifacts",
            tool_name="read_tool_artifact",
        )
    )
    assert reread.text.images == rendered.text.images
    assert "base64" not in reread.text and reread.artifact_id is None
    assert len(tuple((tmp_path / "media").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_media_archive_authorization_manifest_and_exact_read(database, tmp_path):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person, read_scope="main")
    store = ToolArtifactRepository(database, tmp_path / "media", retention_seconds=60)
    result = _result()
    handle = await store.write_media_artifact(
        provider_id=result.provider_id,
        tool_name=result.tool_name,
        images=result.images,
        access=access,
    )
    for operation in ("text", "get", "search", "inspect"):
        manifest = await store.read(handle, operation=operation, access=access)
        assert manifest["image_count"] == 1
        assert "base64" not in json.dumps(manifest)
    assert (await store.read(handle, operation="image"))["error_code"] == "artifact_not_authorized"
    foreign = replace(access, read_scope="plugin.other")
    assert (await store.read(handle, operation="image", access=foreign))["error_code"] == (
        "artifact_not_authorized"
    )
    prepared = await store.read(handle, operation="image", access=access)
    assert isinstance(prepared, PreparedMediaData)
    assert prepared.images[0].data_url == result.images[0].data_url
    assert prepared.images[0].tool_handle == handle
    await store.validate_media(prepared.images, access)
    changed = replace(prepared.images[0], data_url="data:image/jpeg;base64,AA==")
    with pytest.raises(ValueError, match="artifact_media_source_changed"):
        await store.validate_media((changed,), access)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == access.conversation_id)
            .values(generation=2)
        )
    with pytest.raises(ValueError, match="artifact_media_source_changed"):
        await store.validate_media(prepared.images, access)


@pytest.mark.asyncio
async def test_media_archive_expiry_and_corrupt_bytes_fail_closed(database, tmp_path):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "media", retention_seconds=60)
    handle = await store.write_media_artifact(
        provider_id="mcp.browser",
        tool_name="screenshot",
        images=_result().images,
        access=access,
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await store.read(handle, operation="image", access=access) is None
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(expires_at=datetime.now(UTC) + timedelta(seconds=60))
        )
    (tmp_path / "media" / f"{handle}.json").write_text("corrupted")
    assert (await store.read(handle, operation="image", access=access))["error_code"] == (
        "artifact_corrupt"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("replied", [False, True])
async def test_no_implicit_auxiliary_vision_without_native_input(replied):
    processor = object.__new__(MessageProcessor)
    processor._native_images = None
    processor._vision = SimpleNamespace(analyze=AsyncMock())
    attachment = MessageAttachment(
        AttachmentKind.IMAGE, "image", source="reply" if replied else "current"
    )
    message = InboundMessage(
        message_id="transport",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("sender"),
        bot_user_id="bot",
        text="read image",
        attachments=() if replied else (attachment,),
        reply_attachments=(attachment,) if replied else (),
    )
    state = await processor._analyze_visual_input(
        message=message,
        question=message.text,
        source_event_id=1,
        conversation_key="trusted",
        event_key="event",
        sender=object(),
        runtime=SimpleNamespace(vision=None),
    )
    assert state.failed and state.error_code == "image_capability_unavailable"
    assert "未读取" in state.attachment_text
    processor._vision.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_backend_artifact_binding_preserves_native_pixels(database, tmp_path):
    from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    env = await social_env(database, tmp_path)
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    scope = ConversationScope.group(env.bot.self_id, "20001")
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel).limit(1))
    state = await harness.conversation_scopes.get(scope)
    config = await chat._runtime_config.snapshot(group_id="20001")
    store = ToolArtifactRepository(database, tmp_path / "media", retention_seconds=60)
    chat._tool_artifacts = store
    inbound = InboundMessage(
        message_id="inbound",
        text="read selected tool image",
        source_event_id=event.id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        bot_user_id=env.bot.self_id,
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
        conversation_id=env.context.conversation_id,
    )
    async with chat._turn_coordinator.background_turn(scope.key) as token:
        tool_runtime = ToolRuntime(
            inbound=inbound,
            gateway=env.bot,
            allow_generic_onebot=False,
            conversation_key=scope.key,
            runtime_config=config,
            turn_snapshot=ConversationTurnSnapshot(
                state.id,
                scope.key,
                state.generation,
                event.id,
                token.version,
            ),
        )
        access = access_from_runtime(tool_runtime)
        handle = await store.write_media_artifact(
            provider_id="mcp.browser",
            tool_name="screenshot",
            images=_result().images,
            access=access,
        )
        backend = MainAgentBackend(chat, tool_runtime)
        await backend.prepare()
        runtime = SimpleNamespace(
            work_control=None, canonical_conversation_id=env.context.conversation_id
        )
        backend.definitions(runtime, web_was_used=False)
        arguments = json.dumps({"handle": handle, "operation": "image"})
        call = ToolCall("original-read", ToolFunction("read_tool_artifact", arguments))
        # Preserve upstream media assertions through the typed Host boundary.
        invocation = direct_invocations((call,), runtime)[0]
        rendered = await backend.execute_call(invocation)
        assert json.loads(rendered)["ok"] is True, rendered
        assert len(rendered.images) == 1
        assert rendered.images[0].data_url == _result().images[0].data_url
        assert rendered.images[0].tool_handle == handle and "base64" not in rendered
        await backend.validate_images(rendered.images, runtime)
        assert len(tuple((tmp_path / "media").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_disabled_image_profile_uses_native_unread_receipt():
    processor = object.__new__(MessageProcessor)
    processor._native_images = SimpleNamespace(
        images_enabled=False,
        prepare=AsyncMock(return_value=SimpleNamespace(images=(), documents="图片未读取")),
    )
    processor._vision = SimpleNamespace(analyze=AsyncMock())
    message = InboundMessage(
        message_id="transport",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("sender"),
        bot_user_id="bot",
        text="read image",
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image"),),
    )
    state = await processor._analyze_visual_input(
        message=message,
        question=message.text,
        source_event_id=1,
        conversation_key="trusted",
        event_key="event",
        sender=object(),
        runtime=SimpleNamespace(vision=None),
    )
    assert not state.images and state.attachment_text == "图片未读取"
    processor._native_images.prepare.assert_awaited_once()
    processor._vision.analyze.assert_not_awaited()
