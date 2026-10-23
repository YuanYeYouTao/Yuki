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
async def test_explicit_thinking_choice_reaches_actual_vision_request(thinking):
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"items":[{"index":1,"description":"seen"}]}'}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenVisionProvider(
            base_url="https://vision.test/v1", api_key="fixture", client=client
        )
        await provider.analyze(
            visual(), "describe", options=VisionAnalysisOptions(thinking_enabled=thinking)
        )
    assert observed[0]["enable_thinking"] is thinking


@pytest.mark.parametrize("indices", [(1, 2, 3, 4, 5, 6), (1, 1, 3, 7, 5, 6)])
async def test_six_inputs_keep_all_valid_results_and_original_index_association(indices):
    calls = []
    inputs = tuple(
        visual()[0].model_copy(update={"media_hash": f"input-{index}"}) for index in range(6)
    )

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {"index": index, "description": f"seen-{index}"}
                                        for index in indices
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenVisionProvider(
            base_url="https://vision.test/v1", api_key="fixture", client=client
        )
        result = await provider.analyze(inputs, "describe")
    expected = tuple(dict.fromkeys(index for index in indices if index <= len(inputs)))
    assert tuple(item.index for item in result.items) == expected
    assert result.partial_failure is (len(expected) != len(inputs))
    assert len(calls) == 1
