"""Dialect hints for tool screenshots (Computer Use task book §6.1, §17 decided item 6).

Failure cases, written before the dialect code:

- OpenAI (Chat Completions with the ``openai``/``azure_openai`` preset, and Responses) sends
  a ``source="tool"`` image without ``detail:"original"``, so the server may rescale it and
  the model's pixel coordinates refer to another image.
- The hint leaks to chat images (``source`` other than ``tool``) or to OpenAI-compatible
  presets of other vendors, whose endpoints may reject the value.
- Gemini sends ``mediaResolution`` without the Profile asking for it (the default stays the
  server's), or sends it for chat images; with ``tool_image_media_resolution`` set it is
  missing from the tool image's part.
- Anthropic gains ``oversized_image`` or ``transformations`` (not decided).
- Responses ignores the Profile's ``tool_image_detail`` (a class attribute instead of the
  wire option), so it can be neither turned on for a compatible endpoint nor off for OpenAI.
"""

from __future__ import annotations

import base64

from yuki.domain.messages import ChatImage, ChatMessage, ChatRequest
from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.gemini import GeminiProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.openai_responses import OpenAICompatibleResponsesProvider, OpenAIResponsesProvider
from yuki.llm.vendor_policy import ChatWireOptions

PIXEL = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xd9").decode()
COMMON = {
    "base_url": "https://wire.invalid/v1",
    "api_key": "synthetic",
    "timeout_seconds": 1,
    "max_retries": 1,
}


def request() -> ChatRequest:
    return ChatRequest(
        model="m",
        messages=[
            ChatMessage(
                role="user",
                content="look",
                images=(ChatImage(PIXEL, source="tool"), ChatImage(PIXEL, source="current")),
            )
        ],
    )


def chat_images(provider: OpenAICompatibleProvider) -> list[dict[str, object]]:
    payload = provider._build_payload(request())
    content = payload["messages"][-1]["content"]
    return [part["image_url"] for part in content if part["type"] == "image_url"]


def test_openai_chat_completions_marks_only_tool_images_original() -> None:
    tool, chat = chat_images(OpenAICompatibleProvider(provider_name="openai", **COMMON))
    assert tool["detail"] == "original" and "detail" not in chat
    tool, _ = chat_images(OpenAICompatibleProvider(provider_name="azure_openai", **COMMON))
    assert tool["detail"] == "original"
    # Other vendors' compatible endpoints get nothing unless a Profile asks for it.
    for vendor in ("openai_compatible", "qwen", "zhipu", "openrouter"):
        tool, _ = chat_images(OpenAICompatibleProvider(provider_name=vendor, **COMMON))
        assert "detail" not in tool, vendor
    asked = OpenAICompatibleProvider(
        provider_name="qwen", options=ChatWireOptions(tool_image_detail="high"), **COMMON
    )
    assert chat_images(asked)[0]["detail"] == "high"


def test_openai_responses_marks_only_tool_images_original() -> None:
    def images(provider: OpenAIResponsesProvider) -> list[dict[str, object]]:
        payload = provider._build_payload(request())
        parts = [part for item in payload["input"] for part in item.get("content", [])]
        return [part for part in parts if part.get("type") == "input_image"]

    tool, chat = images(OpenAIResponsesProvider(**COMMON))
    assert tool["detail"] == "original" and "detail" not in chat
    assert all("detail" not in part for part in images(OpenAICompatibleResponsesProvider(**COMMON)))
    # The Profile decides, as for Chat Completions.
    asked = OpenAICompatibleResponsesProvider(
        options=ChatWireOptions(tool_image_detail="high"), **COMMON
    )
    assert images(asked)[0]["detail"] == "high"
    quiet = OpenAIResponsesProvider(options=ChatWireOptions(tool_image_detail=None), **COMMON)
    assert all("detail" not in part for part in images(quiet))


def test_gemini_media_resolution_is_a_profile_setting_for_tool_images_only() -> None:
    def parts(provider: GeminiProvider) -> list[dict[str, object]]:
        payload = provider._build_payload(request())
        return [part for part in payload["contents"][-1]["parts"] if "inlineData" in part]

    assert all("mediaResolution" not in part for part in parts(GeminiProvider(**COMMON)))
    tool, chat = parts(
        GeminiProvider(options=ChatWireOptions(tool_image_media_resolution="ultra_high"), **COMMON)
    )
    assert tool["mediaResolution"] == {"level": "MEDIA_RESOLUTION_ULTRA_HIGH"}
    assert "mediaResolution" not in chat


def test_anthropic_images_carry_no_resize_hints() -> None:
    payload = AnthropicMessagesProvider(**COMMON)._build_payload(request())
    images = [
        part
        for message in payload["messages"]
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "image"
    ]
    assert images and all(set(part) == {"type", "source"} for part in images)
