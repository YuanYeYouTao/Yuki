"""Administrator reports contain counts and internal ranges, never source contents."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)
from yuki.social.models import SocialError
from yuki.social.service import SocialContext, SocialService


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
    for key in ("waiting_retry", "isolated", "recent_not_due", "processing"):
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
    async with social.database.read_session() as session:
        event = await session.get(ChatEventModel, cycle["source_event_id"])
        conversation = await session.get(CanonicalConversationModel, cycle["conversation_id"])
        if (
            event is None
            or conversation is None
            or event.canonical_conversation_id != conversation.id
        ):
            return {"status": "failed", "error_category": "reflection_report_source_missing"}
        target = (
            {"space_id": conversation.space_id}
            if conversation.space_id
            else {"person_id": conversation.person_id}
        )
    try:
        return await social.execute(
            "send",
            {
                "target": target,
                "text": format_report(cycle),
            },
            SocialContext(
                turn_id=cycle["id"],
                call_id="final-report",
                conversation_id=cycle["conversation_id"],
                space_id=conversation.space_id,
            ),
        )
    except SocialError as exc:
        receipt = await social.receipts.find(cycle["id"], "final-report")
        if receipt is not None and receipt.status.value != "prepared":
            return receipt.model_dump(mode="json")
        return {"status": "failed", "error_category": str(exc)}


async def self_reflection_weekly_stats(
    database: Database, *, since: date, until: date
) -> list[dict[str, Any]]:
    """Episodes per processed message for completed chat batches, per ISO week (§8.2).

    Batches are bucketed by the local date that starts their ``scheduled_slot``
    (``YYYY-MM-DD:…``, deployment timezone), ``since`` and ``until`` inclusive. The slot
    range uses ``ix_memory_self_reflection_runs_slot``; initiative-receipt batches
    (slot ``self:…``, no messages) fall outside it. Episodes are committed
    ``memory_self_reflection_results`` rows with ``result_kind='episode'``.
    """

    run = MemorySelfReflectionRunModel
    result = MemorySelfReflectionResultModel
    episodes = (
        select(func.count())
        .where(result.run_id == run.id, result.result_kind == "episode")
        .scalar_subquery()
    )
    weeks: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    async with database.read_session() as reader:
        rows = await reader.execute(
            select(run.scheduled_slot, run.processed_events, episodes).where(
                run.scheduled_slot >= since.isoformat(),
                run.scheduled_slot < (until + timedelta(days=1)).isoformat(),
                run.status == "completed",
            )
        )
        for slot, processed, written in rows:
            year, iso_week, _day = date.fromisoformat(slot[:10]).isocalendar()
            weeks[f"{year}-W{iso_week:02d}"].append((int(processed), int(written)))
    report: list[dict[str, Any]] = []
    for week, batches in sorted(weeks.items()):
        processed = sum(count for count, _ in batches)
        written = sum(count for _, count in batches)
        report.append(
            {
                "week": week,
                "batches": len(batches),
                "processed_events": processed,
                "episodes": written,
                # Runs recorded before processed_events existed carry 0.
                "episodes_per_100_events": round(100 * written / processed, 2)
                if processed
                else None,
                "batches_with_3_plus_episodes": sum(count >= 3 for _, count in batches),
                "batches_without_episode": sum(count == 0 for _, count in batches),
            }
        )
    return report
