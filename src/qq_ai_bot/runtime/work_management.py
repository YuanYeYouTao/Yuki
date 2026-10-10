"""Manage an existing Work tree without borrowing its execution authority."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, literal, literal_column, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_repository import TERMINAL, encode_json
from qq_ai_bot.runtime.work_schema_v1 import inputs, journal, work
from qq_ai_bot.runtime.work_tree import descendant_work_ids
from qq_ai_bot.runtime.work_wait_schema import waits

ManagementCode = Literal[
    "not_found",
    "version_conflict",
    "precondition_failed",
    "validation_error",
    "operation_unavailable",
    "state_mismatch",
]


# Source keys a management reader may classify by; never payload bodies.
CLASSIFICATION_KEYS = ("origin", "owner", "initiative_run_id")


class WorkManagementError(Exception):
    def __init__(self, code: ManagementCode) -> None:
        self.code = code
        super().__init__(code)


def require_work_id(identity: str) -> None:
    try:
        if (
            type(identity) is not str
            or str(UUID(identity)) != identity
            or UUID(identity).version != 4
        ):
            raise ValueError("invalid identity")
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkManagementError("validation_error") from exc


async def resume_blocker(
    session: AsyncSession,
    row: Mapping[Any, Any],
    source: Mapping[str, Any] | None,
    now: float,
) -> ManagementCode | None:
    """Read-only resume preconditions shared by ``manage_work`` and Work details.

    ``row`` needs id/state/conversation_id/generation/model_requests; ``source``
    only the origin/owner/initiative_run_id classification (None if invalid).
    Returns the code a resume would raise, or None when the original Work can be
    queued. A delivered wait signal does not unlock a suspended Work by itself.
    """
    if row["state"] not in {"suspended", "waiting_user", "waiting_external", "failed"}:
        return "precondition_failed"
    generation = await session.scalar(
        select(CanonicalConversationModel.generation).where(
            CanonicalConversationModel.id == row["conversation_id"]
        )
    )
    if generation != row["generation"]:
        return "precondition_failed"
    child = (
        (await session.execute(select(children).where(children.c.work_id == row["id"])))
        .mappings()
        .first()
    )
    if child:
        if child["archived_at"] is not None or (
            child["owner"] is not None and child["lease_until"] > now
        ):
            return "precondition_failed"
    else:
        if source is None:
            return "state_mismatch"
        # Each queued Work stays with its original scheduler or run/step owner.
        supported = source.get("owner") in {
            "plugin_invocation",
            "plugin_background",
            "automation",
        } or source.get("origin") in {
            "user_message",
            "autonomous_group",
            "self_initiative",
        }
        if not supported:
            return "operation_unavailable"
        if source.get("origin") == "self_initiative":
            from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel

            # Continue the retained Work through its original run and scene;
            # run settlement does not erase the Work's recovery authority.
            original_run = await session.scalar(
                select(InitiativeRunModel.id).where(
                    InitiativeRunModel.id == source.get("initiative_run_id")
                )
            )
            if original_run is None:
                return "precondition_failed"
    retained = await session.scalar(select(journal.c.work_id).where(journal.c.work_id == row["id"]))
    if row["model_requests"] and retained is None:
        return "state_mismatch"
    return None


async def stop_owned_execution(session: AsyncSession, identity: str, reason: str | None) -> None:
    """Withdraw the selected subtree's authority, preserving original effect receipts.

    The caller publishes the selected Work's final state in its original writer.
    Revocation does not claim any external process has stopped or rolled back.
    """
    tree = await descendant_work_ids(session, identity, include_self=True)
    now = time.time()
    await session.execute(
        update(work)
        .where(work.c.id.in_(tree), work.c.id != identity, work.c.state.not_in(TERMINAL))
        .values(
            state="cancelled",
            reason=reason,
            checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.accepted_control"),
            revision=work.c.revision + 1,
            updated=now,
        )
    )
    await session.execute(
        update(children)
        .where(children.c.work_id.in_(tree))
        .values(owner=None, lease_until=0, cancel_epoch=children.c.cancel_epoch + 1)
    )
    await session.execute(
        update(inputs)
        .where(inputs.c.work_id.in_(tree), inputs.c.state.in_(("pending", "staged")))
        .values(state="cancelled")
    )
    await session.execute(
        update(waits)
        .where(waits.c.work_id.in_(tree), waits.c.status == "active")
        .values(status="cancelled", updated=now)
    )


async def manage_work(
    session: AsyncSession,
    identity: str,
    revision: int,
    action: str,
    *,
    reason: str | None = None,
) -> tuple[int, str]:
    """Caller owns a short writer and its durable management receipt.

    Cancellation prevents new effects. Effects already admitted before this
    transaction keep their original receipts, including late and unknown results.
    Resume queues only the same retained execution; schedulers revalidate its
    original source and authority before any model or effect.
    """
    require_work_id(identity)
    if action not in {"cancel", "fail", "resume"}:
        raise WorkManagementError("validation_error")
    if reason is not None and not isinstance(reason, str):
        raise WorkManagementError("validation_error")
    row = (await session.execute(select(work).where(work.c.id == identity))).mappings().first()
    if row is None:
        raise WorkManagementError("not_found")
    if row["revision"] != revision:
        raise WorkManagementError("version_conflict")
    if row["state"] in TERMINAL and not (action == "resume" and row["state"] == "failed"):
        raise WorkManagementError("precondition_failed")
    now = time.time()
    if action in {"cancel", "fail"}:
        stopped_reason = (
            reason
            if reason is not None
            else "operator_cancelled"
            if action == "cancel"
            else "agent_failed"
        )
        state = "cancelled" if action == "cancel" else "failed"
        await stop_owned_execution(session, identity, stopped_reason)
        await session.execute(
            update(work)
            .where(work.c.id == identity)
            .values(
                state=state,
                reason=stopped_reason,
                checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.accepted_control"),
                revision=work.c.revision + 1,
                updated=now,
            )
        )
        parent_id = row["parent_work_id"]
        if parent_id is not None:
            parent = (
                (await session.execute(select(work).where(work.c.id == parent_id)))
                .mappings()
                .first()
            )
            if parent is not None:
                # The mailbox stores actual stop evidence without granting new
                # input authority or reviving a suspended/terminal parent.
                await session.execute(
                    insert(inputs)
                    .values(
                        conversation_id=parent["conversation_id"],
                        generation=parent["generation"],
                        work_id=parent_id,
                        source_key=f"worker-result:{identity}:{revision + 1}",
                        kind="subagent",
                        ready=True,
                        payload_json=encode_json(
                            {
                                "text": encode_json(
                                    {
                                        "child_id": identity,
                                        "state": state,
                                        "reason": stopped_reason,
                                    }
                                )
                            }
                        ),
                        created=now,
                    )
                    .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
                )
                active_wait = await session.scalar(
                    select(waits.c.id)
                    .where(waits.c.work_id == parent_id, waits.c.status == "active")
                    .limit(1)
                )
                if parent["state"] == "waiting_external" and active_wait is None:
                    await session.execute(
                        update(work)
                        .where(work.c.id == parent_id)
                        .values(state="queued", revision=work.c.revision + 1, updated=now)
                    )
        return revision + 1, state
    try:
        source = json.loads(row["source_json"])
    except (ValueError, TypeError):
        source = None
    blocker = await resume_blocker(session, row, source if isinstance(source, dict) else None, now)
    if blocker is not None:
        raise WorkManagementError(blocker)
    await session.execute(
        update(waits)
        .where(waits.c.work_id == identity, waits.c.status == "active")
        .values(status="cancelled", updated=now)
    )
    await session.execute(
        update(work)
        .where(work.c.id == identity)
        .values(
            state="queued",
            reason="operator_resume",
            checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.accepted_control"),
            revision=work.c.revision + 1,
            updated=now,
        )
    )
    # Keep journal, budgets, inputs, source, failure attempts and not_before.
    return revision + 1, "queued"


async def management_view(session: AsyncSession, identity: str) -> dict[str, Any] | None:
    """Pause reason, delivered signal and original actions of one Work, read-only.

    Reads only lifecycle metadata and the source's classification keys, never
    payload bodies. No state is added: a signal held by a suspended Work stays
    a pending mailbox input until the explicit original resume consumes it.
    """
    row = (
        (
            await session.execute(
                select(
                    work.c.id,
                    work.c.state,
                    work.c.reason,
                    work.c.conversation_id,
                    work.c.generation,
                    work.c.model_requests,
                    *(
                        # Fixed classification paths only; bodies are never read.
                        func.json_extract(work.c.source_json, literal_column(f"'$.{key}'")).label(
                            key
                        )
                        for key in CLASSIFICATION_KEYS
                    ),
                ).where(work.c.id == identity)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    source = {key: row[key] for key in CLASSIFICATION_KEYS}
    terminal = row["state"] in TERMINAL
    resumable = not terminal or row["state"] == "failed"
    blocker = await resume_blocker(session, row, source, time.time()) if resumable else None
    signal = (
        (
            await session.execute(
                select(
                    waits.c.id,
                    waits.c.status,
                    waits.c.delivered,
                    inputs.c.state.label("input_state"),
                )
                .outerjoin(
                    inputs,
                    (inputs.c.work_id == waits.c.work_id)
                    & (inputs.c.source_key == literal("wait:") + waits.c.id),
                )
                .where(
                    waits.c.work_id == identity,
                    waits.c.status.in_(("delivered", "expired")),
                )
                .order_by(waits.c.created.desc(), waits.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    return {
        "pause_reason": row["reason"]
        if row["state"] in {"suspended", "waiting_user", "failed"}
        else None,
        "signal": {
            "wait_id": signal["id"],
            "status": signal["status"],
            "delivered": signal["delivered"],
            # pending: arrived and held for the original owner; consumed: used.
            "input_state": signal["input_state"],
        }
        if signal is not None
        else None,
        "actions": {
            "resume": resumable and blocker is None,
            "resume_blocked_by": blocker,
            "cancel": not terminal,
        },
    }
