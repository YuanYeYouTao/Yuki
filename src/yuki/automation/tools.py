"""Natural-language Agent tools for ordinary users and superusers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar

from yuki.automation.models import (
    AutomationDirectoryEntry,
    AutomationRecord,
    AutomationStatus,
)
from yuki.automation.service import AutomationService
from yuki.capabilities.results import ToolExecutionResult
from yuki.domain.messages import ChatTool
from yuki.persistence.database import write_failure_committed, write_failure_phase
from yuki.services.agent_tools import ToolRuntime
from yuki.time.formatting import local_iso


def _object_schema(
    properties: Mapping[str, object], *, required: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _trigger_schema() -> dict[str, object]:
    """Time arrangement; the backend still validates the real TaskSpec."""

    return {
        "description": "时间安排：after/interval 用 seconds，once 用带时区 local_datetime，"
        "daily/weekly 用 hour、minute（weekly 另给 weekdays 1–7）",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "seconds"],
                "properties": {
                    "type": {"const": "after"},
                    "seconds": {
                        "type": "integer",
                        "minimum": 1,
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "local_datetime"],
                "properties": {
                    "type": {"const": "once"},
                    "local_datetime": {
                        "type": "string",
                        "format": "date-time",
                        "description": "包含 UTC offset 的绝对时刻。",
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "hour", "minute"],
                "properties": {
                    "type": {"const": "daily"},
                    "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                    "timezone": {"type": "string"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "weekdays", "hour", "minute"],
                "properties": {
                    "type": {"const": "weekly"},
                    "weekdays": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    },
                    "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                    "timezone": {"type": "string"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "seconds"],
                "properties": {
                    "type": {"const": "interval"},
                    "seconds": {
                        "type": "integer",
                        "minimum": 1,
                    },
                },
            },
        ],
    }


class AutomationToolService:
    """Expose global safe reads and owner-scoped mutations to the main Agent."""

    _ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"create", "update", "pause", "resume", "cancel", "run_now"}
    )
    _QUERY_ARGUMENTS: ClassVar[frozenset[str]] = frozenset({"id", "history", "limit", "cursor"})
    _MANAGE_ARGUMENTS: ClassVar[dict[str, frozenset[str]]] = {
        "create": frozenset({"action", "goal", "trigger", "name", "max_runs"}),
        "update": frozenset({"action", "id", "goal", "trigger", "name", "max_runs"}),
        **{
            action: frozenset({"action", "id"})
            for action in ("pause", "resume", "cancel", "run_now")
        },
    }

    def __init__(self, service: AutomationService) -> None:
        self._service = service

    def definitions(self) -> tuple[ChatTool, ...]:
        identifier = {"type": "integer", "minimum": 1}
        maximum = {"type": "integer", "minimum": 1}
        cursor = {"type": "string", "pattern": "^[0-9]+$"}
        return (
            ChatTool(
                name="get_schedules",
                namespace="automation.read",
                effect="read_state",
                result_cacheable=False,
                description=(
                    "查看定时安排。不给 id 列出全部安排的摘要（含状态）；给 id 读取完整定义，"
                    "history=true 读取它的历次运行与结果。完整定义只对创建者或超级管理员开放。"
                ),
                parameters=_object_schema(
                    {
                        "id": identifier,
                        "history": {"type": "boolean"},
                        "limit": maximum,
                        "cursor": cursor,
                    }
                ),
            ),
            ChatTool(
                name="manage_schedule",
                namespace="automation.write",
                effect="write_state",
                result_cacheable=False,
                description=(
                    "创建、修改、暂停、恢复、取消或立即运行定时安排。create 与 update 提交完整"
                    " goal 和 trigger，到期由主 Agent 按创建者当前权限执行，消息照常用 send 发出；"
                    "创建时不要提前执行任务。结果含 confirmation=persisted 和 id 才表示已保存。"
                ),
                parameters=_object_schema(
                    {
                        "action": {"type": "string", "enum": sorted(self._ACTIONS)},
                        "id": identifier,
                        "goal": {
                            "type": "string",
                            "minLength": 1,
                            "description": "完整要求与真实收件人线索",
                        },
                        "trigger": _trigger_schema(),
                        "name": {
                            "type": ["string", "null"],
                            "minLength": 1,
                            "description": "公开显示标签；update 省略保留，null 清除",
                        },
                        "max_runs": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "description": "累计运行次数上限；update 省略保留，null 解除",
                        },
                    },
                    required=("action",),
                ),
            ),
        )

    async def execute(
        self, name: str, arguments_json: str, runtime: ToolRuntime
    ) -> ToolExecutionResult:
        if not self._valid_runtime(runtime):
            return _result(error="permission_context_mismatch", detail="自动化工具缺少可信执行主体")
        actor = runtime.require_actor()
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            return _result(error="invalid_arguments", detail=str(exc))
        operation: Any
        if name == "get_schedules":
            allowed = self._QUERY_ARGUMENTS
            operation = (
                "list"
                if "id" not in arguments
                else "history"
                if arguments.get("history") is True
                else "get"
            )
        elif name == "manage_schedule":
            operation = arguments.get("action")
            if not isinstance(operation, str) or operation not in self._ACTIONS:
                return _result(
                    error="invalid_arguments",
                    detail="action 必须是 create、update、pause、resume、cancel 或 run_now",
                )
            allowed = self._MANAGE_ARGUMENTS[operation]
        else:
            return _result(error="unknown_tool", detail=f"未知安排工具：{name}")
        unexpected = set(arguments) - allowed
        if unexpected:
            return _result(
                error="invalid_arguments",
                detail=f"{operation} 不接受参数：{', '.join(sorted(unexpected))}",
            )
        try:
            if operation == "create":
                row = await self._service.create_task(
                    _task(arguments),
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                    max_runs=arguments.get("max_runs"),
                )
                return _result(data=_record(row, persisted=True), mutation_committed=True)
            if operation == "list":
                maximum, offset = _pagination(arguments)
                fetched = await self._service.list_directory(
                    status="all",
                    limit=maximum + 1,
                    offset=offset,
                )
                return _result(
                    data={
                        "schedules": [_directory_record(entry) for entry in fetched[:maximum]],
                        "next_cursor": str(offset + maximum) if len(fetched) > maximum else None,
                    }
                )
            automation_id = _automation_id(arguments)
            if operation == "get":
                row = await self._service.require_manageable(automation_id, actor)
                return _result(data={**_record(row), **_script(row.script)})
            if operation == "update":
                row = await self._service.update_task(
                    automation_id,
                    _task(arguments),
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                    **({"max_runs": arguments["max_runs"]} if "max_runs" in arguments else {}),
                )
                return _result(data=_record(row, persisted=True), mutation_committed=True)
            if operation == "pause":
                changed = await self._service.pause(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "resume":
                changed = await self._service.resume(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "cancel":
                changed = await self._service.cancel(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "run_now":
                changed = await self._service.run_now(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "history":
                maximum, offset = _pagination(arguments)
                task, history_fetched = await self._service.history(
                    automation_id, actor=actor, limit=maximum + 1, offset=offset
                )
                history_rows = history_fetched[:maximum]
                return _result(
                    data={
                        "runs": [
                            {
                                key: value
                                for key, value in {
                                    "run_id": row.id,
                                    "status": row.status.value,
                                    "version_id": row.version_id,
                                    "definition": row.definition,
                                    "result": row.result_summary,
                                    "delivery": row.delivery,
                                    "legacy_messages_sent": row.messages_sent,
                                    "scheduled_for": local_iso(row.scheduled_for, task.timezone),
                                    "finished_at": local_iso(row.finished_at, task.timezone),
                                    "error": row.error_category,
                                }.items()
                                if value is not None
                            }
                            for row in history_rows
                        ],
                        "next_cursor": str(offset + maximum)
                        if len(history_fetched) > maximum
                        else None,
                    }
                )
            else:
                return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
            satisfied: bool | None = None
            if operation == "cancel":
                # A repeated cancel of an already cancelled task meets its request.
                satisfied = changed or (
                    (await self._service.require_manageable(automation_id, actor)).status
                    is AutomationStatus.CANCELLED
                )
            elif operation == "resume":
                satisfied = changed
            return replace(
                _result(
                    data={
                        "id": automation_id,
                        "changed": changed,
                        **({"status": "queued"} if operation == "run_now" and changed else {}),
                    },
                    mutation_committed=changed,
                ),
                request_postcondition_satisfied=satisfied,
            )
        except (PermissionError, ValueError) as exc:
            if write_failure_phase(exc) is not None and write_failure_committed(exc) is not False:
                raise
            return _result(
                error="permission_denied"
                if isinstance(exc, PermissionError)
                else "invalid_arguments",
                detail=str(exc) or "安排参数无效",
            )

    @staticmethod
    def _valid_runtime(runtime: ToolRuntime) -> bool:
        try:
            actor = runtime.require_actor()
            _ = actor.source_key
        except (PermissionError, ValueError):
            return False
        return True


def _pagination(arguments: dict[str, Any]) -> tuple[int, int]:
    maximum = arguments.get("limit", 50)
    if type(maximum) is not int or maximum < 1:
        raise ValueError("limit 必须是正整数")
    cursor = arguments.get("cursor", "0")
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ValueError("cursor 必须是非负整数字符串")
    return maximum, int(cursor)


def _automation_id(arguments: dict[str, Any]) -> int:
    value = arguments.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("id 必须是安排的正整数 id")
    return value


def _task(arguments: dict[str, Any]) -> dict[str, Any]:
    """The flat model arguments as the TaskSpec the service validates."""
    return {key: arguments[key] for key in ("name", "goal", "trigger") if key in arguments}


def _script(script: dict[str, Any]) -> dict[str, Any]:
    return {key: script[key] for key in ("goal", "trigger") if key in script}


def _record(
    row: AutomationRecord,
    *,
    persisted: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "status": row.status.value,
        "timezone": row.timezone,
        "trigger": row.schedule.model_dump(mode="json"),
        "next_run_at": local_iso(row.next_run_at, row.timezone),
        "run_count": row.run_count,
        "max_runs": row.max_runs,
        "version_id": row.version_id,
    }
    if persisted:
        payload["confirmation"] = "persisted"
    return payload


def _directory_record(entry: AutomationDirectoryEntry) -> dict[str, Any]:
    row = entry.record
    creator = entry.creator.model_dump(mode="json")
    if creator["kind"] == "person":
        creator.pop("kind")
    payload: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "next_run_at": local_iso(row.next_run_at, row.timezone),
        "creator": creator,
        "status": row.status.value,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _result(
    *,
    data: object = None,
    error: str | None = None,
    detail: str = "",
    public_message: str | None = None,
    mutation_committed: bool | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=error is None,
        data=data,
        error_code=error,
        public_message=public_message or detail or None,
        mutation_committed=mutation_committed if error is None else False,
    )
