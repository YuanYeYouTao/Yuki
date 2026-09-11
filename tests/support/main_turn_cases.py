"""Shared turn compilation scenarios exercised by the automation integration case."""

import json
from unittest.mock import patch

import pytest

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.compiler import PromptCompiler
from qq_ai_bot.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptProgram,
    PromptTrust,
)
from qq_ai_bot.prompting.serializer import DYNAMIC_ENVELOPE_HEADER, serialize_dynamic


async def run_compiled_state_cases(handlers, state, provider, runtime, context):
    arguments = {"instruction": "compiled once", "context_profile": "none"}
    old_rows = state.snapshot()
    provider._responder = lambda request: "done"
    with patch.object(state, "snapshot", side_effect=[old_rows]) as snapshot:
        composition = await handlers._generation_composition(arguments, context)
        slot = next(row for row in old_rows if row["slot"] == 1)
        updated = state.update(
            {"slot": 1, "text": "next turn state", "expected_revision": slot["revision"]}
        )
        assert updated["ok"]
        await handlers._main_turn_service().run(composition.messages, runtime, None)
        snapshot.assert_called_once_with()
    assert provider.requests[-1].messages == composition.messages
    assert "next turn state" not in composition.messages[-1].content
    assert composition.metrics.total_characters == sum(
        len(message.content or "") for message in composition.messages
    )
    items, _ = json.JSONDecoder().raw_decode(
        composition.messages[-1].content[len(DYNAMIC_ENVELOPE_HEADER) :]
    )
    assert [item["id"] for item in items][-2:] == ["runtime.short_state", "runtime.time"]
    assert items[-2]["data"] == old_rows

    # An empty snapshot is still final for this turn, even if the store now has data.
    with patch.object(state, "snapshot", side_effect=[[]]) as snapshot:
        empty = await handlers._generation_composition(arguments, context)
        await handlers._main_turn_service().run(empty.messages, runtime, None)
        snapshot.assert_called_once_with()
    assert "runtime.short_state" not in provider.requests[-1].messages[-1].content
    later = await handlers._generation_composition(arguments, context)
    assert "next turn state" in later.messages[-1].content
    assert later.messages[0] == composition.messages[0] == empty.messages[0]

    # Tombstones must carry their CAS revision even when no text remains.
    tombstones = [{"slot": 1, "text": "", "revision": 42, "expires_at": 0}]
    with patch.object(state, "snapshot", side_effect=[tombstones]):
        cleared = await handlers._generation_composition(arguments, context)
    assert '"revision":42' in cleared.messages[-1].content

    # Selection measures the envelope, not just its payload. Mandatory state
    # cannot disappear silently to make a request fit.
    contribution = PromptContribution(
        id="runtime.short_state",
        channel=PromptChannel.RUNTIME,
        trust=PromptTrust.UNTRUSTED,
        payload=tombstones,
        required=True,
    )
    compiler = PromptCompiler()
    program = PromptProgram(contributions=(contribution,))
    exact = len(serialize_dynamic((contribution,)))
    with pytest.raises(ValueError, match="required dynamic"):
        compiler.compile(program, dynamic_character_budget=exact - 1)
    compiled = compiler.compile(
        program,
        current_message=ChatMessage(role="user", content="hello"),
        dynamic_character_budget=exact,
    )
    assert compiled.metrics.dynamic_characters == exact
    assert compiled.metrics.total_characters == len(compiled.messages[-1].content)
