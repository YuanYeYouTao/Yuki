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
    if str(observation.get("content", "")).strip() == "NO_REPLY":
        return None
    business = []
    sends = []
    sequence = observation.get("sequence", 0)
    chain_id = control.session.transcript.chain_id
    for result in observation.get("results", []):
        name = result.get("name")
        if not result.get("executed"):
            continue
        key = f"{chain_id}:{sequence}:{result['call_id']}"
        if name in WORK_CONTROL_NAMES:
            try:
                arguments = json.loads(result.get("arguments", "{}"))
            except ValueError:
                continue
            # A child's returned result is business evidence. Lifecycle changes
            # and status polling alone do not create another reporting stage.
            if (
                name != "subagent_control"
                or not isinstance(arguments, dict)
                or arguments.get("action") != "result"
            ):
                continue
            # Re-reading one child's unchanged result is not a new stage merely
            # because another request gave the query a new call identity.
            key = f"subagent_result:{arguments.get('child_id')}"
        try:
            outcome = json.loads(result.get("output", "{}"))
        except ValueError:
            continue
        if isinstance(outcome, dict) and (
            outcome.get("executed") is False
            or (isinstance(outcome.get("data"), dict) and outcome["data"].get("executed") is False)
        ):
            continue
        if name == "send_message":
            try:
                arguments = json.loads(result.get("arguments", "{}"))
            except ValueError:
                continue
            report = arguments.get("work_report") if isinstance(arguments, dict) else None
            if isinstance(report, dict) and report.get("kind") in {"progress", "reply", "final"}:
                sends.append(f"{chain_id}:{sequence}:{result['call_id']}")
            continue
        business.append((key, name, result.get("output")))
    if not business:
        return None
    if sends:
        # Only a confirmed, associated report in this exact batch can replace
        # its opportunity. Ordinary chat and failed attempts are not progress.
        if await control.communication_reports(effect_keys=tuple(sends), delivered_only=True):
            return None
    batch = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
    if batch == control.communication.get("stage_feedback_batch"):
        return None
    return batch, (
        "上一段工具结果仍是内部资料。如果真实结果中的发现、阻塞或目标调整值得向用户说明，"
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
    delivered = set()
    uncertain = set()
    failed = set()
    for report in reports:
        associated = set(report.get("work_report", {}).get("reply_to_event_ids", []))
        if report.get("state") == "accepted" and report.get("delivered_message"):
            delivered.update(associated)
        elif report.get("uncertain") or report.get("pending"):
            uncertain.update(associated)
        else:
            failed.update(associated)
    unanswered = [event_id for event_id in event_ids if event_id not in delivered | uncertain]
    unconfirmed = [event_id for event_id in event_ids if event_id in uncertain - delivered]
    parts = [extra_feedback] if extra_feedback else []
    if unanswered:
        parts.append(
            f"[原 Work 新输入的答复机会 event_ids={unanswered}] "
            "判断哪些输入需要答复或确认调整；需要时调用 send_message，"
            "用 work_report.reply_to_event_ids 关联原内部事件 ID，答复后继续原目标。"
            "补充资料可以不单独发言；这是一次非阻断提醒，已发生的操作不要重复。"
        )
        if failed.intersection(unanswered):
            parts.append(
                "关联发送已有确定失败回执，尚不能视为用户收到答复；按原回执说明阻塞或纠正。"
            )
    if unconfirmed:
        keys = [
            report["effect_key"]
            for report in reports
            if (report.get("uncertain") or report.get("pending"))
            and set(report.get("work_report", {}).get("reply_to_event_ids", [])).intersection(
                unconfirmed
            )
        ]
        parts.append(
            f"[原 Work 关联答复送达未确认 event_ids={unconfirmed} effect_keys={keys}] "
            "先核对原发送回执；未知或仍在执行不等于失败，不能盲目重发。"
            "这是一次非阻断提醒，继续原目标，不把已尝试发送当作已答复。"
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
