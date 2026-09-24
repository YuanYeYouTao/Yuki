"""Identify DSL delivery of model output without interpreting generated text."""

from __future__ import annotations

from collections.abc import Collection

from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.automation.templates import referenced_steps

_MODEL_CAPABILITIES = frozenset({"yuki.agent", "yuki.generate"})
_SEND_CAPABILITIES = frozenset(
    {
        "social.send_message",
        "social.poke_person",
    }
)


def classify_model_delivery(
    script: AutomationScript,
    index: int,
    *,
    send_capabilities: Collection[str] = (),
) -> bool:
    """Follow prior output references and classify a model-dependent send.

    Return true when a DSL SEND depends on model output. Such sends are
    rejected; the Agent must call ``send_message`` inside its own Work.
    """

    if not 0 <= index < len(script.steps):
        raise IndexError("automation step index out of range")
    step = script.steps[index]
    if step.call not in _SEND_CAPABILITIES and step.call not in send_capabilities:
        return False

    # Presence in this map means model-dependent. None marks a derived output;
    # a model step marks an unchanged reference to that step's actual result.
    outputs: dict[str, bool] = {}
    for previous in script.steps[:index]:
        is_model = previous.call in _MODEL_CAPABILITIES
        dependent = is_model or bool(referenced_steps(previous.arguments).intersection(outputs))
        for name in (previous.id, previous.save_as):
            if name is None:
                continue
            if dependent:
                outputs[name] = is_model
            else:
                # Match the executor's latest assignment when a step ID and a
                # previous save_as alias happen to share the same name.
                outputs.pop(name, None)

    if not referenced_steps(step.arguments).intersection(outputs):
        return False
    return True
