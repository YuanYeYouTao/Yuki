"""Small communication policies over the original Work and execution receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from qq_ai_bot.domain.messages import ChatMessage, ToolCall
from qq_ai_bot.runtime.activation_outcome import WorkNoProgress
from qq_ai_bot.runtime.work_control import WORK_CONTROL_NAMES, WorkControl
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def initialize_input_feedback(control: WorkControl) -> None:
    """A newly enabled policy must not replay old consumed conversation input."""
    if (
        control.current is None
        or control.lease.work_id
        or "input_feedback_through_id" in control.communication
    ):
        return
    await control.patch_communication(
        input_feedback_through_id=await control.communication_consumed_watermark()
    )


async def stage_feedback_opportunity(
    control: WorkControl, observation: dict[str, Any]
) -> tuple[str, str] | None:
    """Derive a single reminder from the existing last paired business observation."""
    if (
        control.reporting != "interactive"
        or control.session is None
        or control.session.transcript is None
    ):
        return None
    if str(observation.get("content", "")).strip() in {"", "NO_REPLY"}:
        return None
    business = []
    sends = []
    sequence = observation.get("sequence", 0)
    chain_id = control.session.transcript.chain_id
    for result in observation.get("results", []):
        if not result.get("executed") or result.get("name") in WORK_CONTROL_NAMES:
            continue
        try:
            outcome = json.loads(result.get("output", "{}"))
        except ValueError:
            continue
        if isinstance(outcome, dict) and (
            outcome.get("executed") is False
            or (isinstance(outcome.get("data"), dict) and outcome["data"].get("executed") is False)
        ):
            continue
        if result.get("name") == "send_message":
            sends.append((f"{chain_id}:{sequence}:{result['call_id']}", outcome))
            continue
        business.append(
            (
                f"{chain_id}:{sequence}:{result['call_id']}",
                result["name"],
                result.get("output"),
            )
        )
    if not business:
        return None
    if sends:
        associated = {report["effect_key"] for report in await control.communication_reports()}
        target = await control.communication_target()
        for key, outcome in sends:
            data = outcome.get("data", outcome) if isinstance(outcome, dict) else {}
            if key in associated or (
                isinstance(data, dict)
                and data.get("target") == target
                and data.get("status") in {"succeeded", "failed", "unknown", "uncertain"}
            ):
                return None
    batch = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
    if batch == control.communication.get("stage_feedback_batch"):
        return None
    return batch, (
        "上一段正文仍是内部结果。如果真实结果中的发现、阻塞或目标调整值得向用户说明，"
        "在安全点用 send_message 简述证据、余项和下一步，也可合并到即将发出的最终交付。"
        "没有实质新信息可继续工具，不需逐阶段登记；不重复已成功或结果未知的发送。"
    )


async def before_work_tool(control: WorkControl | None, call: ToolCall) -> str | None:
    """Check controllable business execution before creating an effect or charging it."""
    if control is None or getattr(control, "reporting", None) != "interactive":
        return None
    if call.function.name in {"send_message", "task_control"}:
        return None
    if call.function.name == "subagent_control":
        try:
            arguments = json.loads(call.function.arguments)
        except ValueError:
            arguments = None
        if isinstance(arguments, dict) and arguments.get("action") in {
            "list",
            "status",
            "result",
            "cancel",
        }:
            return None
    if await control.communication_reports(kind="start", delivered_only=True):
        return None
    attempted = await control.communication_reports(kind="start")
    return json.dumps(
        {
            "ok": False,
            "executed": False,
            "error": "work_start_delivery_unconfirmed" if attempted else "work_start_required",
            "detail": (
                "原开始说明交付未确认，本批业务未执行；核对原回执或登记等待/失败，不能盲目重发。"
                if attempted
                else "首次实质执行前先 send_message，并标记 work_report.kind=start；"
                "取得真实送达回执后再提出未执行的业务调用。"
            ),
        },
        ensure_ascii=False,
    )


async def start_feedback_updates(
    control: WorkControl | None, batch: Sequence[tuple[ToolCall, str, bool]]
) -> dict[str, bool]:
    """One correction per Work, after the whole rejected batch has been paired."""
    if control is None or getattr(control, "reporting", None) != "interactive":
        return {}
    rejected = False
    for _call, result, executed in batch:
        if executed:
            continue
        try:
            payload = json.loads(result)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("error") in {
            "work_start_required",
            "work_start_delivery_unconfirmed",
        }:
            rejected = True
    if not rejected or await control.communication_reports(kind="start", delivered_only=True):
        return {}
    if control.communication.get("start_feedback_given"):
        if control.session is not None:
            await control.session.save("paired")
        raise WorkNoProgress("work_start_not_delivered")
    return {"start_feedback_given": True}


async def append_input_feedback(
    control: WorkControl | None,
    transcript: TurnTranscript,
    after_id: int,
    *,
    extra_feedback: str | None = None,
) -> int:
    """Offer one nonblocking reply opportunity, using durable input IDs as the cursor."""
    if control is None or control.current is None or control.lease.work_id:
        return after_id
    if control.source.get("delivery_contract") in {"return_to_caller", "none"}:
        return after_id
    after_id = max(after_id, int(control.communication.get("input_feedback_through_id", 0)))
    items = await control.communication_inputs(after_id=after_id, limit=8)
    if not items:
        if extra_feedback:
            transcript.append(ChatMessage(role="system", content=extra_feedback))
        return after_id
    event_ids = [item["event_id"] for item in items]
    reports = await control.communication_reports(event_ids=tuple(event_ids))
    associated = {
        event_id
        for report in reports
        for event_id in report.get("work_report", {}).get("reply_to_event_ids", [])
    }
    unanswered = [event_id for event_id in event_ids if event_id not in associated]
    parts = [extra_feedback] if extra_feedback else []
    if unanswered:
        parts.append(
            f"[原 Work 新输入的答复机会 event_ids={unanswered}] "
            "判断哪些输入需要答复或确认调整；需要时调用 send_message，"
            "用 work_report.reply_to_event_ids 关联原内部事件 ID，答复后继续原目标。"
            "补充资料可以不单独发言；这是一次非阻断提醒，已发生的操作不要重复。"
        )
    if parts:
        transcript.append(
            ChatMessage(
                role="system",
                content="\n".join(parts),
            )
        )
    return max(item["id"] for item in items)


async def require_interactive_exit(
    control: WorkControl | None, transcript: TurnTranscript, *, extra_feedback: str | None = None
) -> bool:
    """An internal final cannot silently turn an interactive Work into completed."""
    if control is None or getattr(control, "reporting", None) != "interactive" or control.ending:
        return False
    if control.communication.get("final_feedback_given"):
        if control.session is not None:
            await control.session.save("paired")
        raise WorkNoProgress("interactive_work_missing_exit")
    transcript.append(
        ChatMessage(
            role="system",
            content=(extra_feedback + "\n" if extra_feedback else "")
            + (
                "这段正文是内部结果，不能据此结束交互式 Work。"
                "需要交流时用 send_message；随后继续原工具调用，或单独用 task_control "
                "明确 complete/wait/need_input/fail。汇报不是完成，不重复已成功的操作。"
            ),
        )
    )
    if control.session is not None:
        await control.session.save("paired", communication_updates={"final_feedback_given": True})
    else:
        await control.patch_communication(final_feedback_given=True)
    return True
