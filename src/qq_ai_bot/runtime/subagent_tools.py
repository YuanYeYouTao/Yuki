"""Parent/child tools shared by every Work execution."""

from __future__ import annotations

import json
from typing import Any

from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.sandbox.environment_tools import tool


def subagent_tools() -> tuple[ChatTool, ...]:
    string = {"type": "string"}
    return (
        tool(
            "subagent_start",
            "将复杂工作交给后台工作者，立即返回 ID；可以继续聊天。"
            "已有子任务用 control/message，勿重复派生。",
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
            "查询、取消或继续自己的持久子任务；完成后可继续原上下文，已归档会明确返回。"
            "继续原目标直接 resume，可选 instruction 补充要求，无需先 accept 新任务。",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "result", "cancel", "resume"],
                },
                "child_id": string,
                "instruction": string,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "cursor": {"type": "string", "maxLength": 36},
            },
            ("action",),
        ),
        tool(
            "subagent_message",
            "向自己的子任务发送补充、问题或回答；省略 child_id 时联系自己的父任务。"
            "向父任务 ask=true 保存后等待回答，释放执行名额。",
            {
                "child_id": string,
                "text": string,
                "ask": {"type": "boolean"},
                "reply_to": string,
            },
            ("text",),
        ),
    )


async def execute_subagent(
    control: Any, name: str, args: dict[str, Any], key: str
) -> dict[str, Any]:
    repository = SubagentRepository(control.repository)
    if control.current is None:
        if (
            name == "subagent_control"
            and args.get("action") == "resume"
            and isinstance(args.get("child_id"), str)
        ):
            from qq_ai_bot.runtime.work_queries import WorkQueries

            child = await WorkQueries(control.repository).get(
                control.lease, control.source, args["child_id"], local=True
            )
            if child is None or child["parent_work_id"] is None:
                raise ValueError("subagent_archived_or_unknown")
            parent_id = child["parent_work_id"]
        else:
            raise ValueError("accept_work_before_execution")
    else:
        parent_id = control.current["id"]
    if name == "subagent_start":
        if not getattr(control.repository.database, "subagents_enabled", False):
            raise ValueError("subagent_admission_disabled")
        spawned_id = await repository.start(control.lease, parent_id, key, args)
        return {"ok": True, "child_id": spawned_id, "state": "queued", "pending": True}
    identity = args.get("child_id")
    if name == "subagent_message":
        to_parent = identity is None and control.current is not None
        if to_parent:
            identity = control.current["id"]
            parent_id = control.current["parent_work_id"]
            if parent_id is None:
                raise ValueError("subagent_parent_unavailable")
        if not isinstance(identity, str):
            raise ValueError("child_id_required")
        ask = bool(args.get("ask"))
        message_id = await repository.message(
            control.lease,
            parent_id,
            identity,
            key,
            args["text"],
            ask=ask,
            reply_to=args.get("reply_to"),
            to_parent=to_parent,
        )
        if ask and to_parent:
            control.ending = "waiting_user"
        return {"ok": True, "message_id": message_id, "waiting_parent": ask and to_parent}
    action = args.get("action")
    next_cursor = None
    if action == "list":
        limit = args.get("limit", 8)
        cursor = args.get("cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("invalid_subagent_page_limit")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) != 36):
            raise ValueError("invalid_subagent_page_cursor")
        rows = await repository.list(parent_id, limit=limit + 1, cursor=cursor)
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
            "state 是调度状态，tool_calls 是调用次数，不代表已完成修改。"
            "未取得具体命令输出、变更清单或产物验证前，不得声称已删除或清理了一部分。"
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
