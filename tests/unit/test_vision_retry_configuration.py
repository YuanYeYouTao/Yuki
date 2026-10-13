"""Explicit vision retry settings control actual HTTP attempts."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from tests.conftest import make_settings

from qq_ai_bot.vision.base import VisionError
from qq_ai_bot.vision.models import PreparedFrame, PreparedVisualInput
from qq_ai_bot.vision.qwen import QwenVisionProvider


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


@pytest.mark.parametrize("retries", [0, 3])
async def test_zero_disables_retry_and_larger_config_is_honored(retries):
    settings = make_settings("sqlite+aiosqlite:///:memory:", vision_max_retries=retries).vision
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) <= 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"items": [{"index": 1, "description": "observed"}]}
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenVisionProvider(
            base_url="https://vision.test/v1",
            api_key="fixture",
            max_retries=settings.vision_max_retries,
            client=client,
            sleep=AsyncMock(),
        )
        if retries == 0:
            with pytest.raises(VisionError) as error:
                await provider.analyze(visual(), "describe")
            assert error.value.code == "provider_unavailable"
        else:
            result = await provider.analyze(visual(), "describe")
            assert result.items[0].description == "observed"
    assert len(calls) == retries + 1


async def test_authentication_rejection_does_not_buy_retries():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenVisionProvider(
            base_url="https://vision.test/v1", api_key="fixture", max_retries=3, client=client
        )
        with pytest.raises(VisionError) as error:
            await provider.analyze(visual(), "describe")
        assert error.value.code == "authentication_failed"
    assert len(calls) == 1


def test_negative_retry_setting_is_rejected():
    with pytest.raises(ValidationError):
        _ = make_settings("sqlite+aiosqlite:///:memory:", vision_max_retries=-1).vision
