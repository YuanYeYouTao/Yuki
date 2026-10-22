"""An owned send receipt survives removal of its destination's history."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel
from yuki.social.history import read_history
from yuki.social.models import SocialError
from yuki.social.service import SocialContext


def missing_receipt_source():
    source_id, destination_id, operation_id = (str(uuid4()) for _ in range(3))
    operation = SimpleNamespace(
        id=operation_id,
        source_conversation_id=source_id,
        event_id=None,
        original_event_id=912,
        original_conversation_id=destination_id,
        status="succeeded",
    )

    async def get(model, identity):
        if model is CanonicalConversationModel and identity == source_id:
            return SimpleNamespace(id=source_id)
        if model is SocialOperationModel and identity == operation_id:
            return operation
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
    )

    @asynccontextmanager
    async def sessions():
        yield session

    return (
        SimpleNamespace(sessions=sessions),
        SocialContext(turn_id="receipt-read", call_id="receipt", conversation_id=source_id),
        operation,
        session,
    )


@pytest.mark.parametrize("restricted_source", ["delegated", "plugin_background"])
async def test_missing_owned_receipt_remains_metadata_for_restricted_sources(restricted_source):
    database, context, operation, session = missing_receipt_source()
    runtime = (
        SimpleNamespace(read_target_id=str(uuid4()))
        if restricted_source == "delegated"
        else SimpleNamespace(origin="plugin_background")
    )
    result = await read_history(
        database,
        {"view": "around", "target": {"operation_id": operation.id}},
        context,
        runtime=runtime,
    )
    assert result["receipt_status"] == "succeeded"
    assert result["receipt_anchor"] == {
        "event_id": 912,
        "conversation_id": operation.original_conversation_id,
        "status": "missing",
    }
    assert result["events"] == [] and not result["has_more"]
    assert result.source_event_ids == ()
    assert all(call.args[0] is not ChatEventModel for call in session.get.await_args_list)


async def test_missing_destination_does_not_grant_another_sources_receipt():
    database, context, operation, _session = missing_receipt_source()
    operation.source_conversation_id = str(uuid4())
    with pytest.raises(SocialError, match="history_receipt_unavailable"):
        await read_history(
            database,
            {"view": "around", "target": {"operation_id": operation.id}},
            context,
            runtime=SimpleNamespace(origin="plugin_background"),
        )
