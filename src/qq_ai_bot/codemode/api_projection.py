"""Deterministic script API projected from the frozen manifest.

Each wrapper is calling syntax for exactly one canonical tool. The projection
never adds a business definition, alias or permission: the original schema is
the only argument contract, and the reverse map is fixed with the manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.codemode.contract import CODE_API_REVISION, EXECUTE_CODE_NAME
from qq_ai_bot.domain.messages import ChatTool

WRAPPER_PREFIX = "yuki_"
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

# Never projected: recursion into composition, and the lifecycle surfaces a
# script reaches only through the Host control gate (see ``control_kind``).
NEVER_PROJECTED = frozenset({EXECUTE_CODE_NAME})


def encode_wrapper_name(tool_name: str) -> str:
    """Reversible: identifier names map 1:1, others are hex-escaped."""
    if _IDENTIFIER.fullmatch(tool_name) and "__" not in tool_name:
        return WRAPPER_PREFIX + tool_name
    escaped = tool_name.encode().hex()
    return f"{WRAPPER_PREFIX}_x{escaped}"


def decode_wrapper_name(wrapper: str) -> str:
    if not wrapper.startswith(WRAPPER_PREFIX):
        raise ValueError("code_wrapper_name_invalid")
    body = wrapper[len(WRAPPER_PREFIX) :]
    if body.startswith("_x"):
        try:
            return bytes.fromhex(body[2:]).decode()
        except ValueError as exc:
            raise ValueError("code_wrapper_name_invalid") from exc
    if not _IDENTIFIER.fullmatch(body) or "__" in body:
        raise ValueError("code_wrapper_name_invalid")
    return body


@dataclass(frozen=True, slots=True)
class ScriptApi:
    """One frozen projection per manifest revision; holds no actor or runtime."""

    manifest_revision: str
    api_revision: str
    wrappers: dict[str, str]  # wrapper -> canonical tool name
    schemas: dict[str, dict[str, object]]  # canonical tool name -> original schema

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.wrappers)

    def tool_for(self, wrapper: str) -> str | None:
        """Only an exact wrapper of this manifest resolves; nothing else does."""
        return self.wrappers.get(wrapper)

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [self.api_revision, self.manifest_revision, sorted(self.wrappers.items())],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


def project(tools: tuple[ChatTool, ...], manifest_revision: str) -> ScriptApi:
    wrappers: dict[str, str] = {}
    schemas: dict[str, dict[str, object]] = {}
    for tool in tools:
        if tool.name in NEVER_PROJECTED:
            continue
        wrapper = encode_wrapper_name(tool.name)
        if wrapper in wrappers or decode_wrapper_name(wrapper) != tool.name:
            raise ValueError(f"code_wrapper_collision:{tool.name}")
        wrappers[wrapper] = tool.name
        # The original object, unchanged: no coercion, defaults or renaming.
        schemas[tool.name] = tool.parameters
    return ScriptApi(manifest_revision, CODE_API_REVISION, wrappers, schemas)


@dataclass(frozen=True, slots=True)
class ToolReceiptView:
    """What a wrapper returns inside the VM. Never a raw Host exception."""

    status: str  # not_executed / succeeded / failed / pending / unknown
    ok: bool
    data: Any
    error: dict[str, Any] | None
    operation_id: str
    result_ref: str | None
    executed: bool
    reused: bool
    pending: bool
    uncertain: bool
    complete: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "operation_id": self.operation_id,
            "result_ref": self.result_ref,
            "executed": self.executed,
            "reused": self.reused,
            "pending": self.pending,
            "uncertain": self.uncertain,
            "complete": self.complete,
        }


def receipt_view(
    raw: str, *, operation_id: str, executed: bool, reused: bool = False
) -> ToolReceiptView:
    """Project the authoritative receipt string; it is stored, not this view."""
    try:
        value = json.loads(raw)
    except ValueError:
        value = {"ok": False, "error": "tool_result_not_json"}
    if not isinstance(value, dict):
        value = {"ok": True, "data": value}
    ok = value.get("ok") is True
    uncertain = bool(value.get("uncertain"))
    data = value.get("data")
    pending = bool(isinstance(data, dict) and data.get("pending")) or bool(value.get("pending"))
    error_code = value.get("error") or value.get("error_code")
    error = None
    if not ok:
        error = {
            "code": str(error_code or "tool_failed"),
            "message": str(value.get("public_message") or value.get("detail") or "")[:1000],
            "retry": "same_id_query" if uncertain else "never",
        }
    if uncertain:
        status = "unknown"
    elif ok and pending:
        status = "pending"
    elif ok:
        status = "succeeded"
    elif value.get("executed") is False or not executed:
        status = "not_executed"
    else:
        status = "failed"
    truncated = bool(value.get("truncated")) or bool(value.get("result_unavailable"))
    return ToolReceiptView(
        status=status,
        ok=ok,
        data=data if data is not None else _payload_without_envelope(value),
        error=error,
        operation_id=operation_id,
        result_ref=value.get("artifact_handle")
        if isinstance(value.get("artifact_handle"), str)
        else None,
        executed=executed and value.get("executed") is not False,
        reused=reused,
        pending=pending,
        uncertain=uncertain,
        complete=not truncated,
    )


_ENVELOPE = frozenset(
    {
        "ok",
        "error",
        "error_code",
        "public_message",
        "detail",
        "executed",
        "uncertain",
        "replay_forbidden",
        "retryable",
    }
)


def _payload_without_envelope(value: dict[str, Any]) -> dict[str, Any] | None:
    rest = {key: item for key, item in value.items() if key not in _ENVELOPE}
    return rest or None
