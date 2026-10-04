# Portions ported from Pi (https://github.com/earendil-works/pi) at
# 200387122ca450d6387f033949423114a270b96c, packages/agent/src/agent.ts
# (``Agent.processEvents``) and types.ts (``AgentState``).
# MIT License, Copyright (c) 2025 Mario Zechner.
"""Pure event reduction: a derived view, never a durable source of truth.

Work inputs, journal and effects remain authoritative; this state only answers
"what is the loop doing now" for projections and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from qq_ai_bot.agent_core.types import AgentEvent


@dataclass(frozen=True, slots=True)
class AgentState:
    running: bool = False
    streaming: bool = False
    turns: int = 0
    responses: int = 0
    pending_tool_calls: frozenset[str] = frozenset()
    finished_tool_calls: tuple[str, ...] = ()
    truncated_turns: int = 0


def reduce(state: AgentState, event: AgentEvent) -> AgentState:
    """Pi ``processEvents`` transitions, over Yuki's event record."""
    match event.type:
        case "agent_start":
            return replace(state, running=True)
        case "turn_start":
            return replace(state, turns=state.turns + 1)
        case "message_start":
            return replace(state, streaming=True)
        case "message_end":
            return replace(state, streaming=False, responses=state.responses + 1)
        case "tool_execution_start":
            assert event.call is not None
            return replace(state, pending_tool_calls=state.pending_tool_calls | {event.call.id})
        case "tool_execution_end":
            assert event.call is not None
            return replace(
                state,
                pending_tool_calls=state.pending_tool_calls - {event.call.id},
                finished_tool_calls=(*state.finished_tool_calls, event.call.id),
            )
        case "turn_end":
            truncated = event.stop_reason is not None and event.stop_reason.value == "length"
            return replace(state, truncated_turns=state.truncated_turns + truncated)
        case "agent_end":
            return replace(state, running=False, streaming=False)
    return state
