"""Content-free diagnostics computed from the final HTTP JSON, not a prompt proxy."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.runtime.observability import current_runtime_turn_correlation

logger = logging.getLogger(__name__)


def wire_hash(value: object) -> str:
    # Preserve array and object insertion order, as the outgoing JSON does.
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class WireFingerprint:
    instructions: str
    tools: str
    settings: str
    inputs: tuple[str, ...]

    @classmethod
    def of(cls, payload: dict[str, Any], protocol: str) -> WireFingerprint:
        if protocol == "responses":
            instructions = payload.get("instructions")
            inputs = payload.get("input", [])
        else:
            messages = payload.get("messages", [])
            boundary = 0
            while boundary < len(messages) and messages[boundary].get("role") in {
                "system",
                "developer",
            }:
                boundary += 1
            instructions, inputs = messages[:boundary], messages[boundary:]
        settings = {
            k: v
            for k, v in payload.items()
            if k not in {"instructions", "input", "messages", "tools"}
        }
        return cls(
            wire_hash(instructions),
            wire_hash(payload.get("tools")),
            wire_hash(settings),
            tuple(wire_hash(item) for item in inputs),
        )


class WireRequestObserver:
    """Bounded per-provider comparison; holds hashes only, never prompt bodies."""

    def __init__(self) -> None:
        self._recent: OrderedDict[tuple[str, str], WireFingerprint] = OrderedDict()

    def observe(
        self, payload: dict[str, Any], protocol: str, *, chain_id: str = ""
    ) -> dict[str, object]:
        correlation = current_runtime_turn_correlation()
        current = WireFingerprint.of(payload, protocol)
        # A runtime turn can contain independent internal model tasks. Compare only
        # the explicit Agent transcript, not arbitrary requests sharing an actor.
        key = (chain_id, protocol) if chain_id else None
        previous = self._recent.get(key) if key else None
        changes: list[str] = []
        first_difference: int | None = None
        relation = "unbound" if key is None else "first_observation"
        if previous is not None:
            for name in ("instructions", "tools", "settings"):
                if getattr(current, name) != getattr(previous, name):
                    changes.append(name)
            common = min(len(previous.inputs), len(current.inputs))
            first_difference = next(
                (i for i in range(common) if previous.inputs[i] != current.inputs[i]), None
            )
            if first_difference is None and len(current.inputs) < len(previous.inputs):
                first_difference = len(current.inputs)
            if first_difference is not None:
                relation = "input_rewritten"
            elif len(current.inputs) > len(previous.inputs):
                relation = "append"
            else:
                relation = "same_input"
        if key:
            self._recent[key] = current
            self._recent.move_to_end(key)
            while len(self._recent) > 64:
                self._recent.popitem(last=False)
        result: dict[str, object] = {
            "correlation_id": correlation.turn_id if correlation else "unbound",
            "protocol": protocol,
            "stage": "dispatch_attempt",
            "chain_hash": wire_hash(chain_id) if chain_id else "unbound",
            "origin": correlation.origin.value if correlation else "unknown",
            "instructions_hash": current.instructions,
            "tools_hash": current.tools,
            "settings_hash": current.settings,
            "input_hash": wire_hash(current.inputs),
            "input_items": len(current.inputs),
            "relation": relation,
            "changed_fields": changes,
            "first_difference_index": first_difference,
        }
        logger.info("provider_wire_request %s", json.dumps(result, separators=(",", ":")))
        return result
