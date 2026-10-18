"""Optional vision calls make one actual HTTP request and retain thinking options."""

import json

import httpx
import pytest
from tests.conftest import make_settings

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


def test_vision_retry_configuration_is_retired():
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    assert "vision_max_retries" not in type(settings).model_fields
    assert "vision_max_retries" not in type(settings.vision).model_fields


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
