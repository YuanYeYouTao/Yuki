"""Media guidance stays in the fixed prefix without rewriting the user's task."""

import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image
from tests.conftest import make_settings

from yuki.domain.messages import ChatMessage
from yuki.services.context_assembler import AssembledContext, ContextMetrics
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.native_media import NativeMediaPreparer
from yuki.services.prompt_composer import PromptComposer


@pytest.mark.parametrize("code_enabled", (False, True))
def test_media_guidance_preserves_casual_and_explicit_image_tasks(code_enabled):
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(data, format="PNG")
    images = NativeMediaPreparer(ImagePreprocessor()).prepare_image(
        data.getvalue(), source="current"
    )
    current = ChatMessage(role="user", content="哈哈哈 [图片附件0]", images=images)
    history = (ChatMessage(role="user", content="[Ada] [表情: 好耶]"),)
    context = AssembledContext(
        metadata_payload={"scene": "群聊", "platform": "qq"},
        history_messages=history,
        current_message=current,
        metrics=ContextMetrics(0, 0, 1, len(current.content), False),
    )
    composer = PromptComposer(
        make_settings("sqlite+aiosqlite:///:memory:", code_mode_enabled=code_enabled)
    )
    explicit = replace(current, content="请读出图片中的文字并解释这个错误 [图片附件0]")
    casual = composer.compose(inbound=None, context=context, runtime=SimpleNamespace())
    requested = composer.compose(
        inbound=None,
        context=replace(context, current_message=explicit),
        runtime=SimpleNamespace(),
    )
    casual_system = tuple(item for item in casual.messages if item.role == "system")
    assert casual_system == tuple(item for item in requested.messages if item.role == "system")
    instructions = "\n".join(item.content or "" for item in casual_system)
    for guidance in (
        "表情顺着聊天语气",
        "按当前话题和任务需要选读",
        "原事件编号、附件序号及共享缓存位置",
        "inspect_conversation_attachment 或终端",
        "emoji.goal",
        "后台选图无需等候",
    ):
        assert instructions.count(guidance) == 1
    for composition, source in ((casual, current), (requested, explicit)):
        assert composition.messages[-1].images == images
        assert composition.messages[-1].content.endswith(source.content)
        assert composition.messages[-2] == history[0]
        assert "emoji.goal" not in (composition.messages[-1].content or "")
    assert explicit.content not in instructions and "哈哈哈" not in instructions
