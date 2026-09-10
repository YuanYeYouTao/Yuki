"""Content-free evidence diagnostics; never an authorization or truth oracle."""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4

from qq_ai_bot.domain.messages import ChatTool, NativeToolDefinition
from qq_ai_bot.runtime.observability import current_runtime_turn_correlation

logger = logging.getLogger(__name__)

EVIDENCE_TOOLS = frozenset(
    {
        "get_person_memories",
        "get_group_memories",
        "get_self_memories",
        "get_memory_fact",
        "get_memory_evidence",
        "web_search",
        "read_webpage",
        "search_chat_history",
        "get_recent_chat_history",
        "get_chat_history_around",
    }
)


class EvidenceObservation:
    """One runner invocation, with no account, query, source URL or content logging."""

    def __init__(self, origin: str) -> None:
        self.correlation_id = str(uuid4())
        self.origin = origin
        correlation = current_runtime_turn_correlation()
        self.runtime_turn_id = correlation.turn_id if correlation is not None else None

    def emit(self, phase: str, **fields: str | int | bool) -> None:
        try:
            logger.info(
                "agent_evidence %s",
                json.dumps(
                    {
                        "correlation_id": self.correlation_id,
                        "runtime_turn_id": self.runtime_turn_id,
                        "origin": self.origin,
                        "phase": phase,
                        **fields,
                    },
                    sort_keys=True,
                ),
            )
        except Exception:
            # Broken telemetry must not convert a valid read into a failed tool.
            try:
                logging.getLogger(__name__).handle(
                    logging.LogRecord(
                        __name__,
                        logging.WARNING,
                        __file__,
                        0,
                        "evidence_observation_failed",
                        (),
                        None,
                    )
                )
            except Exception:
                pass  # Even the diagnostic handler can be unavailable.

    def request(
        self,
        index: int,
        definitions: tuple[ChatTool, ...],
        native: tuple[NativeToolDefinition, ...],
        *,
        finalization: bool,
        web_mode: str,
    ) -> None:
        shape = [(tool.name, tool.description, tool.parameters) for tool in definitions]
        fingerprint = hashlib.sha256(
            json.dumps(
                {"functions": shape, "native": [tool.type.value for tool in native]}, sort_keys=True
            ).encode()
        ).hexdigest()
        self.emit(
            "request_prepared",
            request_index=index,
            schema_fingerprint=fingerprint,
            evidence_tools=",".join(
                sorted(tool.name for tool in definitions if tool.name in EVIDENCE_TOOLS)
            ),
            native_web=bool(native),
            web_mode=web_mode,
            finalization=finalization,
        )
