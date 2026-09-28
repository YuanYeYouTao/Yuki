"""Finite operator workspace actions using the existing store and Linux Manager.

Control intents anchor operator terminals. They do not create chat sources or
Agent continuations; Manager completions are recorded by the existing receiver.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from qq_ai_bot.control_plane.command_types import ManagementActionPayload
from qq_ai_bot.control_plane.commands import ControlCommand
from qq_ai_bot.control_plane.json_types import JsonValue
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ActivityView, ControlQueryError
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.sandbox.client import SandboxClient, sandbox_tools
from qq_ai_bot.workspace.files import MAX_CONTROL_UPLOAD, FileWorkspace
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceError
from qq_ai_bot.workspace.tools import workspace_tools

MAX_UPLOAD_BYTES = 640 * 1024
FILE_ACTIONS = {
    name: f"workspace_{name}" for name in ("write", "mkdir", "move", "delete", "patch", "publish")
}
FILE_ACTIONS["upload"] = "workspace_upload"
TERMINAL_ACTIONS = {name: f"terminal_{name}" for name in ("exec", "write", "control")}
SCHEMAS = {tool.name: tool.parameters for tool in (*workspace_tools(), *sandbox_tools())}


def plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


def file_schema(method: str) -> dict[str, Any]:
    """Restrict the original dual file/artifact contract to the Control file route."""
    if method == "workspace_upload":
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "maxLength": 4096},
                "base64": {"type": "string", "maxLength": (MAX_CONTROL_UPLOAD + 2) // 3 * 4},
                "expected_version": {"type": "string"},
            },
            "required": ["path", "base64", "expected_version"],
            "additionalProperties": False,
        }
    schema: dict[str, Any] = plain(SCHEMAS[method])
    for key in (
        "artifact_id",
        "expected_revision",
        *(("name",) if method == "workspace_write" else ()),
    ):
        schema["properties"].pop(key, None)
    schema["required"] = list(dict.fromkeys([*schema.get("required", []), "path"]))
    return schema


def path_parts(path: str) -> tuple[str, ...]:
    try:
        return FileWorkspace.parts(path)
    except WorkspaceError as exc:
        raise ValueError("invalid workspace path") from exc


def arguments(parsed: ManagementActionPayload, *, terminal: bool = False) -> dict[str, Any]:
    methods = TERMINAL_ACTIONS if terminal else FILE_ACTIONS
    method = methods.get(parsed.action)
    if method is None or parsed.spec is None:
        raise ValueError("unsupported workspace action")
    args: dict[str, Any] = plain(parsed.spec)
    schema = SCHEMAS[method] if terminal else file_schema(method)
    if next(Draft202012Validator(schema).iter_errors(args), None) is not None:
        raise ValueError("invalid workspace arguments")
    if terminal:
        if parsed.action != "exec":
            RequestId.parse(args["run_id"])
    else:
        if "path" not in args or "artifact_id" in args:
            raise ValueError("file action requires a workspace path")
        path_parts(args["path"])
        if "destination" in args:
            path_parts(args["destination"])
        if parsed.action == "upload":
            try:
                content = base64.b64decode(args["base64"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid upload") from exc
            if len(content) > MAX_CONTROL_UPLOAD:
                raise ValueError("upload too large")
    return args


def upload(parsed: ManagementActionPayload) -> tuple[str, bytes]:
    spec = plain(parsed.spec or {})
    if set(spec) != {"name", "base64"} or not all(type(v) is str for v in spec.values()):
        raise ValueError("invalid upload")
    name = spec["name"]
    if not name or len(name) > 128 or any(c in name for c in "\\/:\x00") or name in {".", ".."}:
        raise ValueError("invalid artifact name")
    if len(spec["base64"]) > (MAX_UPLOAD_BYTES + 2) // 3 * 4:
        raise ValueError("upload too large")
    data = base64.b64decode(spec["base64"], validate=True)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("upload too large")
    return name, data


class ControlWorkspace:
    def __init__(
        self, workspace: WorkspaceService | None, *, readonly_root: Path | None = None
    ) -> None:
        self.workspace = workspace
        self.readonly_root = readonly_root

    def transport(self) -> SandboxClient:
        if self.workspace is None or self.workspace.sandbox is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        # Same socket and Manager; control receipt replaces chat-task ownership.
        return SandboxClient(self.workspace.sandbox.socket)

    async def read_submission(
        self, request_id: RequestId, principal_id: PrincipalId
    ) -> ActivityView:
        result = await self.transport().execute(
            "get_code_run_by_request",
            {"request_id": f"control:{principal_id.text}:{request_id.text}"},
            request_id=RequestId.new().text,
        )
        if len(json.dumps(result, ensure_ascii=False).encode()) > 256 * 1024:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        return ActivityView(request_id.text, result)

    async def read(self, section: str, args: Mapping[str, JsonValue]) -> ActivityView:
        if section == "schema":
            return ActivityView(
                "environment",
                {
                    "file_actions": {
                        action: file_schema(method) for action, method in FILE_ACTIONS.items()
                    },
                    "terminal_actions": {
                        action: SCHEMAS[method] for action, method in TERMINAL_ACTIONS.items()
                    },
                    "max_upload_bytes": MAX_UPLOAD_BYTES,
                },
            )
        data = plain(args)
        if section == "files" and "number" in data:
            if self.readonly_root is None:
                raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
            if set(data) - {"path", "limit", "number"}:
                raise ValueError("unexpected file listing arguments")
            result = await asyncio.to_thread(
                FileWorkspace(self.readonly_root).listing,
                data.get("path", ""),
                limit=data.get("limit", 30),
                number=data["number"],
            )
            return ActivityView("environment", result)
        if section == "status":
            if data:
                raise ValueError("unexpected status arguments")
            method = "environment_status"
        elif section in {"files", "file", "terminal"}:
            method = {
                "files": "workspace_list",
                "file": "workspace_read",
                "terminal": "terminal_read",
            }[section]
            if next(Draft202012Validator(SCHEMAS[method]).iter_errors(data), None) is not None:
                raise ValueError("invalid query arguments")
            if section in {"files", "file"}:
                if "artifact_id" in data or (section == "file" and "path" not in data):
                    raise ValueError("workspace path required")
                path_parts(data.get("path", ""))
            else:
                RequestId.parse(data["run_id"])
        else:
            raise ValueError("unknown environment view")
        result = await self.transport().execute(method, data, request_id=RequestId.new().text)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 256 * 1024:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        if section == "status":
            result = {
                key: result[key]
                for key in (
                    "ready",
                    "storage",
                    "limits",
                    "runs",
                    "services",
                    "package_layer_bytes",
                    "package_budget_bytes",
                    "host_available_bytes",
                    "error",
                )
                if key in result
            }
        return ActivityView("environment", result)

    async def mutate(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        parsed: ManagementActionPayload,
        operation: str,
    ) -> tuple[str, int, str]:
        if self.workspace is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        if operation == "control.workspace.mutate":
            if parsed.action == "upload":
                name, data = upload(parsed)
                result = await self.workspace.upload_file(
                    name, data, request_id=command.request_id.text
                )
            elif parsed.action == "edit":
                result = await self.workspace.execute(
                    "workspace_write",
                    {
                        **plain(parsed.spec or {}),
                        "artifact_id": parsed.resource_id,
                        "expected_revision": command.expected_revision,
                    },
                    request_id=command.request_id.text,
                )
            else:
                await self.workspace.execute(
                    "workspace_delete",
                    {
                        "artifact_id": parsed.resource_id,
                        "expected_revision": command.expected_revision,
                    },
                    request_id=command.request_id.text,
                )
                return parsed.resource_id, command.expected_revision + 1, "deleted"
            return (
                result["artifact_id"],
                result["revision"],
                "saved_import_failed" if result.get("file_error") else "saved",
            )
        terminal = operation == "control.terminal.mutate"
        args = arguments(parsed, terminal=terminal)
        method = (TERMINAL_ACTIONS if terminal else FILE_ACTIONS)[parsed.action]
        request = (
            f"control:{principal.principal_id.text}:{command.request_id.text}"
            if terminal
            else command.request_id.text
        )
        result = await self.transport().execute(method, args, request_id=request)
        error = result.get("error")
        if error:
            if error in {
                "sandbox_submission_unknown",
                "sandbox_unavailable",
                "file_mutation_unknown_read_current_version",
            }:
                raise RuntimeError("external workspace effect unknown")
            raise WorkspaceError(str(error))
        resource = str(result["run_id"]) if terminal else parsed.resource_id
        return resource, 1, "accepted" if terminal else "saved"
