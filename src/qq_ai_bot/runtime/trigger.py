"""Host-built triggers for turns that are not caused by a live inbound message.

Each shape carries only trusted host facts (SELF initiative, plugin external
event, sandbox completion, work resume); synthetic inbound messages are
forbidden by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qq_ai_bot.runtime.errors import InvalidTurnTriggerError
from qq_ai_bot.runtime.origin import TurnOrigin

_TARGET_TYPES = frozenset({"group", "private"})


@dataclass(frozen=True, slots=True)
class SelfInitiativeTrigger:
    """Host-accepted SELF intent, with no borrowed person or synthetic event."""

    run_id: str
    conversation_id: str
    generation: int
    space_id: str
    presence_id: str
    group_id: str
    bot_user_id: str
    instruction: str
    origin: TurnOrigin = field(default=TurnOrigin.SELF_INITIATIVE, init=False)

    def __post_init__(self) -> None:
        if self.generation < 1 or any(
            not value.strip()
            for value in (
                self.run_id,
                self.conversation_id,
                self.space_id,
                self.presence_id,
                self.group_id,
                self.bot_user_id,
                self.instruction,
            )
        ):
            raise InvalidTurnTriggerError("invalid self initiative trigger")


@dataclass(frozen=True, slots=True)
class ExternalEventTurnTrigger:
    """A plugin-background turn caused by an external event (outbox job)."""

    plugin_id: str
    source_event_id: int
    target_type: str
    target_id: str
    agent_intent: str = ""
    origin: TurnOrigin = field(default=TurnOrigin.PLUGIN_BACKGROUND)

    def __post_init__(self) -> None:
        if self.origin is not TurnOrigin.PLUGIN_BACKGROUND:
            raise InvalidTurnTriggerError("external event trigger origin must be plugin_background")
        if not self.plugin_id:
            raise InvalidTurnTriggerError("external event trigger requires a plugin id")
        if self.target_type not in _TARGET_TYPES:
            raise InvalidTurnTriggerError(
                f"unknown external event target type: {self.target_type!r}"
            )
        if not self.target_id:
            raise InvalidTurnTriggerError("external event trigger requires a target id")
        if len(self.agent_intent) > 1_000:
            raise InvalidTurnTriggerError("external event trigger intent is too long")


@dataclass(frozen=True, slots=True)
class SandboxTaskTurnTrigger:
    """A real backend completion, with authority retained by its original source."""

    source_event_id: int
    target_type: str
    target_id: str
    completion_payload: str = ""
    agent_intent: str = "Continue the original task using the completed sandbox results."
    origin: TurnOrigin = field(default=TurnOrigin.SYSTEM_TASK, init=False)
    plugin_id: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.source_event_id <= 0 or self.target_type not in _TARGET_TYPES or not self.target_id:
            raise InvalidTurnTriggerError("invalid sandbox completion trigger")


@dataclass(frozen=True, slots=True)
class WorkResumeTrigger:
    """Host scheduling signal referencing the original admitted message."""

    source_event_id: int
    target_type: str
    target_id: str
    agent_intent: str = "Resume the existing work from its durable inputs and execution receipts."
    origin: TurnOrigin = field(default=TurnOrigin.SYSTEM_TASK, init=False)
    plugin_id: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.source_event_id <= 0 or self.target_type not in _TARGET_TYPES or not self.target_id:
            raise InvalidTurnTriggerError("invalid work resume trigger")
