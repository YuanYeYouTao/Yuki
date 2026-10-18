"""Main can preserve a real attachment and index it without visual inspection."""

import asyncio
import base64
import io
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.integration.test_telegram_attachment_tools import attachment_tools  # noqa: F401

from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    AttachmentKind,
    ChatResponse,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.emoji import sticker_index
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.sandbox.persistent import PersistentManager
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.media_resolver import MediaResolver
from yuki.workspace.service import WorkspaceService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


@pytest.mark.skipif(os.name != "posix", reason="Manager workspace FDs require POSIX")
async def test_save_original_then_terminal_update_collects_without_inspection(
    attachment_tools,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    case = attachment_tools
    manager = PersistentManager(
        tmp_path / "manager",
        case.store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    case.workspace.sandbox = SimpleNamespace(execute=manager.file_request)
    inspect = AsyncMock(side_effect=AssertionError("collection does not inspect pixels"))
    monkeypatch.setattr(case.media, "inspect", inspect)
    destination = "stickers/telegram/files/主动收藏.png"
    isolated = tmp_path / "runtime"
    isolated.mkdir()
    helper = isolated / "sticker_index.py"
    shutil.copyfile(sticker_index.__file__, helper)
    shutil.copyfile(Path(sticker_index.__file__).with_name("media.py"), isolated / "media.py")

    async def terminal(*arguments):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(helper),
            "--workspace",
            str(manager.files.root),
            *arguments,
            cwd=isolated,
            env={**os.environ, "PYTHONPATH": ""},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        assert process.returncode == 0, error.decode()
        return json.loads(output)

    try:
        saved = await case.workspace.execute(
            "save_conversation_attachment_to_workspace",
            {
                "event_id": case.source.id,
                "attachment_index": 0,
                "destination": destination,
            },
            runtime=case.runtime,
            request_id="main-collect-original",
        )
        assert saved["file_imported"]
        original = manager.files.root / destination
        assert original.read_bytes() == case.original_bytes
        assert not (manager.files.root / "stickers/telegram/index").exists()
        assert case.wire == [
            ("POST", "/botfixture-a/getFile"),
            ("GET", "/file/botfixture-a/photos/original.png"),
        ]

        updated = await terminal("update", destination, "--tag", "开心", "--tag", "反应")
        asset = updated["asset"]
        assert asset["relative_path"] == destination and asset["tags"] == ["开心", "反应"]
        assert json.loads(
            (manager.files.root / f"stickers/telegram/index/{asset['id']}.json").read_text()
        )["tags"] == ["开心", "反应"]
        selected = await terminal("search", "--platform", "telegram", "开心")
        assert [item["id"] for item in selected["items"]] == [asset["id"]]
        inspect.assert_not_awaited()
        assert len(case.wire) == 2
    finally:
        await manager.close()


@pytest.mark.parametrize("uploaded_image", [False, True])
async def test_main_reads_an_uploaded_file_only_after_its_explicit_tool_call(
    database, tmp_path, monkeypatch, uploaded_image
):
    from yuki.conversation import media_service

    secret = "only-the-original-file-has-this-content"
    filename, content = "notes.txt", secret.encode()
    if uploaded_image:
        picture = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(picture, "PNG")
        filename, content = "uploaded-as-file.png", picture.getvalue()
    committed = []
    parser = AsyncMock(wraps=media_service.run_parser)
    monkeypatch.setattr(media_service, "run_parser", parser)
    auxiliary = AsyncMock(side_effect=AssertionError("ordinary input never calls Qwen"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", auxiliary)

    def respond(request):
        if len(provider.requests) == 1:
            assert not any(message.images for message in request.messages)
            assert secret not in str(request.messages)
            visible = "\n".join(message.content or "" for message in request.messages)
            assert filename in visible and "文件附件0" in visible
            assert f"#{committed[0].event.id}" in visible
            parser.assert_not_awaited()
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "read-original-file",
                        ToolFunction(
                            "inspect_conversation_attachment",
                            json.dumps(
                                {
                                    "event_id": committed[0].event.id,
                                    "attachment_index": 0,
                                    "question": "读取这份原附件",
                                }
                            ),
                        ),
                    ),
                ),
            )
        return "NO_REPLY"

    provider = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), provider)
    resolver = MediaResolver()
    media = ConversationMediaService(database, tmp_path / "cache", resolver, ImagePreprocessor())
    workspace = WorkspaceService(WorkspaceStore(tmp_path / "artifacts"))
    workspace.conversation_media = media
    harness.processor._conversation_media = media
    chat = harness.processor._chat
    chat._tools.workspace_service = workspace
    state = ShortState(workspace.store)
    state.update({"slot": 1, "text": "file reading test"})
    chat._tools.short_state = state
    chat.runtime.runner.main_contract = MainAgentContract(chat, state, code_enabled=False)
    chat.runtime.runner.code_mode_settings = harness.settings
    locator = "base64://" + base64.b64encode(content).decode()
    incoming = InboundMessage(
        message_id="file-is-data-not-a-reading-command",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        bot_user_id="8000",
        text="",
        attachments=(
            MessageAttachment(AttachmentKind.FILE, filename, filename=filename, file=locator),
        ),
        segments=({"type": "file", "data": {"name": filename, "file": locator}},),
    )
    sender = MemorySender()
    try:
        result = await harness.processor.handle(incoming, sender, on_committed=committed.append)
        assert result.reason == "chat" and len(provider.requests) == 2
        assert sender.calls == 0
        auxiliary.assert_not_awaited()
        if uploaded_image:
            assert any(message.images for message in provider.requests[1].messages)
            parser.assert_not_awaited()
        else:
            assert secret in str(provider.requests[1].messages)
            assert parser.await_count == 1
        async with database.sessions() as reader:
            public = await reader.scalar(
                select(ChatEventModel.content).where(ChatEventModel.id == committed[0].event.id)
            )
        assert secret not in public
    finally:
        await media.close()
        await resolver.close()
