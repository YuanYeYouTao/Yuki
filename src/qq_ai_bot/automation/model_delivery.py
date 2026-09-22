"""Identify DSL delivery of model output without interpreting generated text."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from qq_ai_bot.automation.models import AutomationScript, AutomationStep
from qq_ai_bot.automation.templates import referenced_steps

_MODEL_CAPABILITIES = frozenset({"yuki.agent", "yuki.generate"})
_SEND_CAPABILITIES = frozenset(
    {
        "onebot.send_private_message",
        "onebot.send_group_message",
        "speech.send_private",
        "speech.send_group",
        "emoji.send",
        "emoji.send_by_id",
        "social.send_message",
        "social.poke_person",
        # A generic API dispatch consuming model output is not proven to be a
        # read. It must not bypass the explicit Agent delivery boundary.
        "onebot.call_api",
    }
)


@dataclass(frozen=True, slots=True)
class ModelDelivery:
    """A model-dependent send; only direct legacy text has a receipt owner."""

    source_step: AutomationStep | None = None
    target: Literal["self_private", "current_group"] | None = None

    @property
    def can_retire_by_receipt(self) -> bool:
        """Whether the executor may check the original work's target receipts."""

        return self.source_step is not None and self.target is not None


def classify_model_delivery(
    script: AutomationScript,
    index: int,
    *,
    send_capabilities: Collection[str] = (),
) -> ModelDelivery | None:
    """Follow prior output references and classify a model-dependent send.

    ``None`` means ordinary DSL output. A result without a source/target must
    be blocked, never sent as a fallback. A direct result only identifies the
    old work whose durable receipts can retire the step; it is not permission
    to transmit the saved text. Registered plugin SEND names are supplied by
    callers so their output paths obey the same boundary.
    """

    if not 0 <= index < len(script.steps):
        raise IndexError("automation step index out of range")
    step = script.steps[index]
    if step.call not in _SEND_CAPABILITIES and step.call not in send_capabilities:
        return None

    # Presence in this map means model-dependent. None marks a derived output;
    # a model step marks an unchanged reference to that step's actual result.
    outputs: dict[str, AutomationStep | None] = {}
    for previous in script.steps[:index]:
        is_model = previous.call in _MODEL_CAPABILITIES
        dependent = is_model or bool(referenced_steps(previous.arguments).intersection(outputs))
        for name in (previous.id, previous.save_as):
            if name is None:
                continue
            if dependent:
                outputs[name] = previous if is_model else None
            else:
                # Match the executor's latest assignment when a step ID and a
                # previous save_as alias happen to share the same name.
                outputs.pop(name, None)

    if not referenced_steps(step.arguments).intersection(outputs):
        return None

    text = step.arguments.get("text")
    references = referenced_steps(text)
    if len(references) != 1:
        return ModelDelivery()
    name = next(iter(references))
    source = outputs.get(name)
    if source is None or text != "${" + name + ".text}":
        return ModelDelivery()

    # Only the old plain-text builtin targets have an unambiguous receipt
    # target. Speech, generic API, plugin, transformed and indirect sends stop.
    if (
        step.call == "onebot.send_private_message"
        and set(step.arguments) == {"user_id", "text"}
        and step.arguments["user_id"] == "$creator_user_id"
    ):
        return ModelDelivery(source, "self_private")
    if (
        step.call == "onebot.send_group_message"
        and set(step.arguments) == {"group_id", "text"}
        and step.arguments["group_id"] == "$current_group_id"
    ):
        return ModelDelivery(source, "current_group")
    return ModelDelivery()
