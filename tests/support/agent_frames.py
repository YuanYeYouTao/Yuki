from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Literal

from qq_ai_bot.agent_core.types import AgentEvent
from qq_ai_bot.domain.messages import ChatResponse, ModelResponseStatus, ToolCall, ToolFunction

# --- Synthetic frames (tests and boundary contracts only) -------------------
#
# Yuki providers currently return complete responses; this assembly exists so
# partial-response invariants are executable without claiming vendor streaming.


@dataclass(frozen=True, slots=True)
class Frame:
    """An immutable synthetic response fragment for boundary tests."""

    type: Literal["start", "text_delta", "toolcall_delta", "done", "error"]
    text: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


async def collect_response(
    frames: AsyncIterator[Frame], emit: Callable[[AgentEvent], None]
) -> ChatResponse:
    """Assemble synthetic fragments; only a ``done`` frame completes a response.

    A stream that ends early or with ``error`` yields an INCOMPLETE response,
    which the loop never executes. Emitted payloads are frozen copies, so a
    listener never sees a later partial state.
    """
    partial = ChatResponse("", 0, status=ModelResponseStatus.INCOMPLETE)
    arguments: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    started = False
    async for frame in frames:
        if frame.type == "start":
            started = True
            emit(AgentEvent("message_start", response=partial))
            continue
        if not started:
            raise ValueError("frame_before_start")
        if frame.type == "text_delta":
            partial = replace(partial, content=partial.content + frame.text)
        elif frame.type == "toolcall_delta":
            names.setdefault(frame.call_id, frame.name)
            arguments.setdefault(frame.call_id, []).append(frame.arguments)
        calls = tuple(
            ToolCall(call_id, ToolFunction(names[call_id], "".join(parts)))
            for call_id, parts in arguments.items()
        )
        partial = replace(partial, tool_calls=calls)
        if frame.type in {"done", "error"}:
            if frame.type == "done":
                partial = replace(partial, status=ModelResponseStatus.COMPLETED)
            else:
                partial = replace(partial, incomplete_reason="stream_error")
            break
        emit(AgentEvent("message_start", response=partial))
    else:
        partial = replace(partial, incomplete_reason="stream_ended")
    emit(AgentEvent("message_end", response=partial))
    return partial
