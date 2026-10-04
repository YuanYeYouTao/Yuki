"""Persist and restore engine dumps only through the original Work's private store."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qq_ai_bot.codemode.driver_types import EngineCall, HostCounters
from qq_ai_bot.runtime.protocol_store import CodeSnapshotBinding

if TYPE_CHECKING:
    from qq_ai_bot.runtime.protocol_store import ProtocolStore

# The engine's own dump header (`MONTY\0` + format version, docs/snapshots.md).
_MONTY_MAGIC = b"MONTY\x00"


@dataclass(frozen=True, slots=True)
class BoundaryRecord:
    """What the parent composition saves next to the snapshot reference."""

    snapshot_ref: str
    feed_index: int
    call: EngineCall
    counters: dict[str, int]

    def composition_fields(self) -> dict[str, Any]:
        return {
            "snapshot_ref": self.snapshot_ref,
            "feed_index": self.feed_index,
            "boundary_kind": self.call.kind,
            "boundary_call": _call_json(self.call),
            "resource_used": self.counters,
        }


def _call_json(call: EngineCall) -> dict[str, Any]:
    return {
        "kind": call.kind,
        "feed_index": call.feed_index,
        "engine_call_id": call.engine_call_id,
        "function_name": call.function_name,
        "args_digest": call_digest(call),
        "pending_call_ids": sorted(call.pending_call_ids),
    }


def call_digest(call: EngineCall) -> str:
    """Matches a re-announced call to the saved boundary; never a business identity."""
    encoded = json.dumps(
        [call.function_name, list(call.args), call.kwargs],
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def expected_call(composition: dict[str, Any]) -> EngineCall:
    """Rebuild the saved boundary for re-announcement matching."""
    saved = composition.get("boundary_call")
    if not isinstance(saved, dict):
        raise ValueError("snapshot_binding_conflict")
    return EngineCall(
        kind=saved["kind"],
        feed_index=int(saved["feed_index"]),
        engine_call_id=saved["engine_call_id"],
        function_name=saved["function_name"],
        pending_call_ids=tuple(saved.get("pending_call_ids", ())),
    )


def matches_saved(saved: dict[str, Any], announced: EngineCall) -> bool:
    if saved.get("kind") != announced.kind:
        return False
    if announced.kind == "future":
        return sorted(saved.get("pending_call_ids", ())) == sorted(announced.pending_call_ids)
    return (
        saved.get("engine_call_id") == announced.engine_call_id
        and saved.get("function_name") == announced.function_name
        and saved.get("args_digest") == call_digest(announced)
    )


async def persist_boundary(
    store: ProtocolStore,
    binding: CodeSnapshotBinding,
    dump: bytes,
    call: EngineCall,
    counters: HostCounters,
    *,
    max_bytes: int,
) -> BoundaryRecord:
    """T0: prepare the private object; publication happens in WorkRepository T1."""
    if not dump.startswith(_MONTY_MAGIC):
        raise ValueError("code_snapshot_format_mismatch")
    if len(dump) > max_bytes:
        raise ValueError("code_snapshot_capacity")
    snapshot_ref = await store.put_code_snapshot(binding, dump)
    return BoundaryRecord(snapshot_ref, call.feed_index, call, counters.as_dict())


async def load_boundary(
    store: ProtocolStore, binding: CodeSnapshotBinding, composition: dict[str, Any]
) -> tuple[bytes, EngineCall, HostCounters]:
    """Only an owned, binding-matched private object may reach the native loader."""
    snapshot_ref = composition.get("snapshot_ref")
    if not isinstance(snapshot_ref, str):
        raise ValueError("snapshot_binding_conflict")
    dump = await store.get_code_snapshot(snapshot_ref, binding)
    if not dump.startswith(_MONTY_MAGIC):
        raise ValueError("code_snapshot_format_mismatch")
    counters = HostCounters.from_dict(composition.get("resource_used", {}))
    return dump, expected_call(composition), counters
