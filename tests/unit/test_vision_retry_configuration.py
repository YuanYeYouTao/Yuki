"""Optional vision calls make one actual HTTP request and retain thinking options."""

import json

import httpx
import pytest

from yuki.vision.base import VisionError
from yuki.vision.models import PreparedFrame, PreparedVisualInput, VisionAnalysisOptions
from yuki.vision.qwen import QwenVisionProvider


def visual():
    return (
        PreparedVisualInput(
            media_hash="fixture",
            animated=False,
            source="current",
            frames=(
                PreparedFrame(
                    content_hash="fixture",
                    mime_type="image/png",
                    width=1,
                    height=1,
                    frame_index=0,
                    frame_count=1,
                    data_url="data:image/png;base64,aW1hZ2U=",
                ),
            ),
        ),
    )


@pytest.mark.parametrize("failure", [401, 429, 503, "timeout", "connect"])
async def test_vision_failure_makes_one_http_request(failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        if failure == "connect":
            raise httpx.ConnectError("fixture", request=request)
        return httpx.Response(failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenVisionProvider(
            base_url="https://vision.test/v1", api_key="fixture", client=client
        )
        with pytest.raises(VisionError):
            await provider.analyze(visual(), "describe")
    assert len(calls) == 1


@pytest.mark.parametrize("thinking", [False, True])
async def test_six_inputs_preserve_all_results_indices_and_explicit_thinking(thinking):
    inputs = tuple(
        visual()[0].model_copy(update={"media_hash": f"input-{index}"}) for index in range(6)
    )
    indices = (1, 2, 3, 4, 5, 6, 1, 7) if thinking else (1, 1, 3, 7, 5, 6)
    body = json.dumps(
        {
            "items": [
                {"index": index, "description": f"seen-{index}"}
                for index in indices
            ]
        }
    )

    def respond(request):
        payload = json.loads(request.content)
        assert payload["enable_thinking"] is thinking
        assert sum(item["type"] == "image_url" for item in payload["messages"][1]["content"]) == 6
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenVisionProvider(
            base_url="https://vision.test/v1", api_key="fixture", client=client
        )
        result = await provider.analyze(
            inputs, "describe", options=VisionAnalysisOptions(thinking_enabled=thinking)
        )
    expected = (1, 2, 3, 4, 5, 6) if thinking else (1, 3, 5, 6)
    assert tuple(item.index for item in result.items) == expected
    assert result.partial_failure is (not thinking)
