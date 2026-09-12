"""Forwarded data stays bounded, event-bound and separate from the speaker."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_ai_bot.adapters.onebot.normalizer import project_serialized_segments
from qq_ai_bot.services.forwarded_inputs import expand_forwarded


async def check_forwarded_inputs(service, message, vision, image_data):
    projection = project_serialized_segments(
        [{"type": "forward", "data": {"id": "real-forward-id"}}],
        yuki_account_ids=frozenset(),
        source="reply",
    )
    assert projection.attachments[0].file == "real-forward-id"
    calls = []

    async def call_api(action, args):
        calls.append((action, args))
        return {
            "data": {
                "messages": [
                    {
                        "sender": {"nickname": "转发作者"},
                        "content": [
                            {"type": "text", "data": {"text": "forwarded unique fact"}},
                            {"type": "image", "data": {"file": image_data}},
                            {"type": "forward", "data": {"id": "real-forward-id"}},
                        ],
                    }
                ]
            }
        }

    gateway = SimpleNamespace(call_api=call_api)
    media, text = await expand_forwarded(projection.attachments, gateway)
    assert "forwarded unique fact" in text and "非当前发言者" in text
    assert len(media) == 1 and media[0].source == "reply"
    assert len(calls) == 1  # cycle is not fetched twice
    assert "截断" in text
    service.images_enabled = True
    prepared = await service.prepare(
        replace(message, attachments=(), reply_attachments=projection.attachments),
        vision,
        gateway,
    )
    assert prepared.images and "forwarded unique fact" in prepared.documents
    assert all(frame.source == "reply" for frame in prepared.images)
    no_gateway = await expand_forwarded(projection.attachments, None)
    assert not no_gateway[0] and "未读取" in no_gateway[1]

    async def huge(action, args):
        return {"messages": [{"content": "长" * 10000} for _ in range(100)]}

    _, bounded = await expand_forwarded(projection.attachments, SimpleNamespace(call_api=huge))
    assert len(bounded) <= 20000 and "截断" in bounded

    async def cancelled(action, args):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await expand_forwarded(projection.attachments, SimpleNamespace(call_api=cancelled))
