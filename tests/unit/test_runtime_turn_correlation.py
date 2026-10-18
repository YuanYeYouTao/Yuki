"""The original turn identity survives without a second tail database write."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from tests.conftest import MemorySender, build_harness, make_settings

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.runtime.observability import (
    RuntimeTurnCorrelation,
    bind_runtime_turn,
    claim_runtime_turn_id,
    current_runtime_turn_correlation,
)
from yuki.runtime.origin import TurnOrigin
from yuki.services.processor import ProcessResult


@pytest.mark.parametrize("failed", [False, True])
async def test_message_exit_preserves_result_or_error_and_does_no_tail_sql(
    database, monkeypatch, failed
):
    harness = build_harness(database, make_settings(database.url))
    processor = harness.processor
    message = InboundMessage(
        message_id="original-input",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        bot_user_id="8000",
        text="original input",
    )
    monkeypatch.setattr(
        processor._canonical_ingress,
        "pre_admit",
        AsyncMock(return_value=SimpleNamespace(dropped=False, message=message)),
    )
    result = ProcessResult(True, sent_messages=1, reason="chat")
    problem = RuntimeError("original error")
    observed = []

    async def admitted(incoming, _sender, **_kwargs):
        assert incoming is message
        correlation = current_runtime_turn_correlation()
        assert correlation.origin is TurnOrigin.USER_MESSAGE
        assert claim_runtime_turn_id() == correlation.turn_id
        correlation.phase_seconds["inbound_admission_seconds"] = 0.25
        observed.append(correlation)
        if failed:
            raise problem
        return result

    monkeypatch.setattr(processor, "_handle_admitted", admitted)
    statements = []

    def capture(_connection, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    parent = RuntimeTurnCorrelation("parent-execution", TurnOrigin.SYSTEM_TASK)
    try:
        with bind_runtime_turn(parent):
            if failed:
                with pytest.raises(RuntimeError) as caught:
                    await processor.handle(message, MemorySender())
                assert caught.value is problem
            else:
                assert await processor.handle(message, MemorySender()) is result
            assert current_runtime_turn_correlation() is parent
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(observed) == 1 and observed[0].turn_id != parent.turn_id
    assert observed[0].phase_seconds == {"inbound_admission_seconds": 0.25}
    assert claim_runtime_turn_id() is None
    assert statements == []
