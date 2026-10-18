"""Read actual current chunks for assertions about public frozen bytes."""

import json
from types import SimpleNamespace

from sqlalchemy import select

from yuki.conversation.projection_models import PromptProjectionChunkModel


async def projection_record(session, row):
    items = []
    for payload in await session.scalars(
        select(PromptProjectionChunkModel.payload_json)
        .where(
            PromptProjectionChunkModel.view_key == row.view_key,
            PromptProjectionChunkModel.epoch_id == row.epoch_id,
        )
        .order_by(PromptProjectionChunkModel.ordinal)
    ):
        items.extend(json.loads(payload))
    return SimpleNamespace(
        **{column.name: getattr(row, column.name) for column in row.__table__.columns},
        payload_json=json.dumps(items, ensure_ascii=False, separators=(",", ":")),
    )
