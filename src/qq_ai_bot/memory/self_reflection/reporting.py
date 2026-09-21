"""Administrator reports contain counts and internal ranges, never source contents."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.social.service import SocialContext, SocialService


def format_report(cycle: dict[str, Any], *, page: int = 1) -> str:
    before = cycle.get("before", {})
    after = cycle.get("after", before)
    a = before.get("actionable", {})
    b = after.get("actionable", {})
    calls, limit = after.get("calls_today", 0), after.get("daily_limit", 0)
    lines = [
        f"Self Reflection {cycle['id']} [{cycle['status']}] ({cycle['trigger']})",
        f"登记 {cycle['created_at']}；"
        f"开始 {cycle.get('started_at') or '待执行'}；"
        f"结束 {cycle.get('completed_at') or '尚未结束'}",
        f"可执行积压 {a.get('events', 0)} -> {b.get('events', 0)} 事件；"
        f"{a.get('conversations', 0)} -> {b.get('conversations', 0)} 会话。",
        f"今日模型请求 {calls}/{limit}，剩余 {max(0, limit - calls)}（修复和传输重试也计费）。",
    ]
    if "duration_seconds" in cycle:
        lines.append(f"耗时 {cycle['duration_seconds']:.1f} 秒。")
    if "bounds" in cycle:
        lines.append(f"本轮边界：{cycle['bounds']}。")
    if "attempted_batches" in cycle:
        lines += [
            f"批次：尝试 {cycle['attempted_batches']}，成功 {cycle['completed_batches']}，"
            f"失败 {cycle['failed_batches']}，延后 {cycle['deferred_batches']}。",
            f"成功覆盖 {cycle['processed_events']} 事件/{cycle['processed_characters']} 字符；"
            f"proposal {cycle['proposal_count']}，写入 {cycle['committed_count']}。",
            f"原因 {cycle['reason']}；边界 {cycle['limit_flags']}；错误 {cycle['errors']}。",
        ]
    for key in ("waiting_retry", "isolated", "policy_ineligible", "recent_not_due", "processing"):
        entry = after.get(key, {})
        lines.append(f"{key}: {entry.get('events', 0)} 事件/{entry.get('conversations', 0)} 会话")
    failures = cycle.get("failures", [])
    for row in failures[(page - 1) * 5 : page * 5]:
        lines.append(
            f"批次 {row['id']}，事件 {row['first_event_id']}–{row['last_event_id']}："
            f"{row['error']}；"
            f"检查点未推进；"
            f"{row['retry_state']}；"
            f"下次 {row['next_attempt_at']}。"
        )
    if failures:
        lines.append(
            f"失败项第 {page}/{(len(failures) + 4) // 5} 页；"
            f"/ai memory self-reflection status {cycle['id']} <页码>"
        )
    else:
        lines.append(f"/ai memory self-reflection status {cycle['id']}")
    return "\n".join(lines)


async def deliver_report(social: SocialService, cycle: dict[str, Any]) -> dict[str, Any]:
    # Reuse the existing prepared/accepted/uncertain social effect receipt.
    prior = await social.receipts.find(cycle["id"], "final-report")
    if prior is not None and prior.status.value != "prepared":
        return prior.model_dump(mode="json")
    async with social.database.sessions() as session:
        event = await session.get(ChatEventModel, cycle["source_event_id"])
        conversation = await session.get(CanonicalConversationModel, cycle["conversation_id"])
        if (
            event is None
            or conversation is None
            or event.canonical_conversation_id != conversation.id
        ):
            raise ValueError("reflection_report_source_missing")
        target = conversation.space_id or conversation.person_id
        target_kind = "space" if conversation.space_id else "person"
    return await social.execute(
        "send_message",
        {
            "target": {"kind": target_kind, "target_id": target},
            "text": format_report(cycle),
        },
        SocialContext(
            turn_id=cycle["id"],
            call_id="final-report",
            conversation_id=cycle["conversation_id"],
            space_id=conversation.space_id,
        ),
    )
