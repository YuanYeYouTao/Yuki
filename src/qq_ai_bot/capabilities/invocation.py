"""Authority-bound invocation context for the unified Tool Kernel."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from qq_ai_bot.domain.messages import ToolCall


@dataclass(frozen=True, slots=True)
class InvocationIdentity:
    """Host-owned identity; arguments are deliberately absent from its key."""

    operation_id: str
    owner_execution_id: str
    chain_id: str
    request_sequence: int
    provider_call_id: str
    parent_operation_id: str | None = None
    child_ordinal: int | None = None
    engine_call_id: str | None = None
    feed_index: int | None = None


@dataclass(frozen=True, slots=True)
class TrustedInvocationContext:
    """A host object reference, never a dictionary accepted from generated code."""

    runtime: Any
    manifest_revision: str


@dataclass(frozen=True, slots=True)
class Invocation:
    identity: InvocationIdentity
    call: ToolCall
    context: TrustedInvocationContext

    def __post_init__(self) -> None:
        if self.identity.provider_call_id != self.call.id:
            raise ValueError("invocation_call_identity_conflict")

    def durable_metadata(self) -> dict[str, Any]:
        """Only business arguments are canonicalized; Provider opaque is untouched."""
        arguments = self.call.function.arguments
        try:
            arguments = json.dumps(
                json.loads(arguments),
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (ValueError, TypeError):
            # Invalid input still has a stable conflict fingerprint, never a
            # different identity or an implicit coercion into valid arguments.
            pass
        identity = asdict(self.identity)
        identity["parent_effect_key"] = identity.pop("parent_operation_id")
        return {
            "version": 1,
            **identity,
            "tool_id": self.call.function.name,
            "arguments_digest": hashlib.sha256(arguments.encode()).hexdigest(),
            "manifest_revision": self.context.manifest_revision,
            "dispatch_started": False,
            "budget_admitted": False,
            "revision": 0,
        }


def direct_operation_id(chain_id: str, request_sequence: int, provider_call_id: str) -> str:
    """Keep historical short keys; bind long IDs by an unambiguous full tuple."""
    legacy = f"{chain_id}:{request_sequence}:{provider_call_id}"
    if len(legacy.encode()) <= 256:
        return legacy
    encoded = json.dumps(
        ["yuki.invocation.direct.v1", chain_id, request_sequence, provider_call_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return "invocation:v1:" + hashlib.sha256(encoded).hexdigest()


def direct_invocations(
    calls: tuple[ToolCall, ...],
    runtime: Any,
    *,
    chain_id: str = "",
    request_sequence: int = 0,
    manifest_revision: str = "",
) -> tuple[Invocation, ...]:
    """Bind a model response to its original journal, or a turn-local Host identity."""
    control = getattr(runtime, "work_control", None)
    session = getattr(control, "session", None)
    current = getattr(control, "current", None)
    if session is not None:
        chain_id = session.transcript.chain_id
        request_sequence = session.sequence
    # Unowned, non-durable calculations may have an ephemeral activation identity.
    # This never looks up a platform message or creates a durable Work/source.
    owner = (
        str(current["id"])
        if current is not None
        else str(getattr(runtime, "execution_id", None) or uuid4())
    )
    chain_id = chain_id or str(uuid4())
    context = TrustedInvocationContext(runtime, manifest_revision)
    return tuple(
        Invocation(
            InvocationIdentity(
                operation_id=direct_operation_id(chain_id, request_sequence, call.id),
                owner_execution_id=owner,
                chain_id=chain_id,
                request_sequence=request_sequence,
                provider_call_id=call.id,
            ),
            call,
            context,
        )
        for call in calls
    )


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    """Runtime values that providers may consume but a model can never supply."""

    runtime: Any
    call_id: str = ""
    conversation_key: str = ""
    actor_user_id: str = ""
    trigger_message_id: str = ""
    execution_id: str = ""
    provider_metadata: dict[str, Any] | None = None

    @property
    def execution_key(self) -> str:
        identity = self.execution_id or getattr(self.runtime, "effective_execution_id", None)
        identity = identity or getattr(self.runtime, "execution_id", None)
        if not identity:
            raise ValueError("missing_internal_execution_anchor")
        return str(identity)


current_invocation: ContextVar[ToolInvocationContext | None] = ContextVar(
    "tool_invocation", default=None
)
