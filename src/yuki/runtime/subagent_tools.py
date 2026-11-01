"""Parent/child tools shared by every Work execution."""

from __future__ import annotations

import json
from typing import Any, cast

from yuki.domain.messages import ChatTool
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_queries import WorkQueries
from yuki.sandbox.environment_tools import tool


def subagent_tools() -> tuple[ChatTool, ...]:
    string = {"type": "string"}
    return (
        tool(
            "subagent_start",
            "将复杂工作交给后台工作者，立即返回 child_id；父任务可继续处理其他输入。"
            "需要当前 Work；尚未登记时先用 task_control accept 父任务。"
            "派出的子任务已经登记工作，直接执行；结果由最终正文或 complete.result 回传父任务。"
            "仍需这个子任务的结果且当前无可独立推进的工作时，"
            "用 task_control(action=wait, run_id=child_id) 等待。"
            "父任务按原目标整合结果和处理交付；子任务结束不代表父目标已经完成。"
            "已有子任务用 subagent_control，勿重复派生。",
            {
                "goal": {"type": "string"},
                "context": string,
                "acceptance": string,
                "files": {"type": "array", "items": string},
            },
            ("goal",),
        ),
        tool(
            "subagent_control",
            "按原 child_id 查询、取消或恢复自己的持久子任务；已归档会明确返回。"
            "已有子任务直接按 ID 管理，无需先 accept 新任务；"
            "resume 用 instruction 继续；message 用 text 补充或提问，省略 child_id 联系父任务。"
            "向父 ask=true 保存问题并按回执等待；reply_to 回答原问题。",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "result", "cancel", "resume", "message"],
                },
                "child_id": string,
                "instruction": string,
                "text": string,
                "ask": {"type": "boolean"},
                "reply_to": string,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "cursor": {"type": "string", "maxLength": 36},
            },
            ("action",),
        ),
    )


async def execute_subagent(
    control: Any, name: str, args: dict[str, Any], key: str
) -> dict[str, Any]:
    repository = SubagentRepository(control.repository)
    if name == "subagent_start":
        if control.current is None:
            raise ValueError("accept_work_before_execution")
        spawned_id = await repository.start(control.lease, control.current["id"], key, args)
        return {"ok": True, "child_id": spawned_id, "state": "queued", "pending": True}
    identity = args.get("child_id")
    to_parent = args.get("action") == "message" and identity is None
    if to_parent:
        if control.current is None or control.current["parent_work_id"] is None:
            raise ValueError("subagent_parent_unavailable")
        identity = control.current["id"]
        parent_id = control.current["parent_work_id"]
    elif args.get("action") != "list":
        if not isinstance(identity, str):
            raise ValueError("child_id_required")
        child = await WorkQueries(control.repository).get(
            control.lease, control.source, identity, local=True
        )
        if child is None or child["parent_work_id"] is None:
            raise ValueError("subagent_not_owned")
        parent_id = control.lease.work_id or child["parent_work_id"]
    else:
        parent_id = control.lease.work_id
    if args.get("action") == "message":
        ask = bool(args.get("ask"))
        receipt = await repository.message(
            control.lease,
            parent_id,
            cast(str, identity),
            key,
            args.get("text"),
            ask=ask,
            reply_to=args.get("reply_to"),
            to_parent=to_parent,
        )
        if receipt["waiting_parent"]:
            await control._accept("need_input", key, reason=args.get("text"))
        return {"ok": True, **receipt}
    action = args.get("action")
    next_cursor = None
    if action == "list":
        limit = args.get("limit", 8)
        cursor = args.get("cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("invalid_subagent_page_limit")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) != 36):
            raise ValueError("invalid_subagent_page_cursor")
        rows = await repository.list(
            parent_id, lease=control.lease, source=control.source, limit=limit + 1, cursor=cursor
        )
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1]["work_id"]
    else:
        if not isinstance(identity, str):
            raise ValueError("child_id_required")
        if action == "cancel":
            await repository.cancel(control.lease, parent_id, identity)
        elif action == "resume":
            await repository.resume(
                control.lease,
                parent_id,
                identity,
                key=key,
                instruction=args.get("instruction"),
            )
        elif action not in {"status", "result"}:
            raise ValueError("invalid_subagent_action")
        rows = [
            await repository.related(parent_id, identity, include_checkpoint=action == "result")
        ]
    return {
        "ok": True,
        "next_cursor": next_cursor,
        "evidence_note": (
            "状态和调用次数不证明任务成果；具体结果以原工具回执、产物和投递结果为准。"
        ),
        "children": [
            {
                "child_id": row["work_id"],
                "state": (
                    "archived"
                    if row["archived_at"]
                    else "waiting_parent"
                    if row["state"] == "waiting_user"
                    else row["state"]
                ),
                "goal": row["goal"],
                "model_requests": row["model_requests"],
                "tool_calls": row["tool_calls"],
                "result": {
                    k: v for k, v in json.loads(row["result_json"]).items() if k != "cache_samples"
                },
            }
            for row in rows
        ],
    }
