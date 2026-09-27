"""Operator terminal completion receipts; never wake an Agent or create a chat event."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel


async def record_control_completion(database: Database, event: dict[str, Any]) -> None:
    prefix, principal, request = str(event["request_id"]).split(":")
    if prefix != "control":
        raise ValueError("invalid control completion")
    PrincipalId.parse(principal)
    RequestId.parse(request)
    run_id = RequestId.parse(event["run_id"]).text
    result = event.get("result")
    if (
        not isinstance(result, dict)
        or result.get("run_id") != run_id
        or result.get("pending") is not False
        or result.get("status") not in {"succeeded", "failed", "cancelled"}
    ):
        raise ValueError("invalid control terminal completion")
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > 240000:
        raise ValueError("control completion too large")
    payload = json.dumps(
        {
            "run_id": run_id,
            "status": result["status"],
            "sha256": hashlib.sha256(encoded).hexdigest(),
        },
        sort_keys=True,
    )
    async with database.immediate_session() as session:
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.principal_id == principal,
                ControlCommandReceiptModel.request_id == request,
            )
        )
        audit = await session.get(AdminOperationEventModel, receipt.audit_id) if receipt else None
        if (
            audit is None
            or audit.operation != "control.terminal.mutate"
            or audit.actor_principal_id != principal
            or audit.control_request_id != request
            or (
                receipt is not None
                and receipt.status == "succeeded"
                and receipt.result_resource_id != run_id
            )
        ):
            raise ValueError("control terminal intent missing")
        prior = await session.scalar(
            select(AdminOperationEventModel).where(
                AdminOperationEventModel.operation == "control.terminal.completion",
                AdminOperationEventModel.actor_principal_id == principal,
                AdminOperationEventModel.control_request_id == request,
            )
        )
        if prior is not None:
            if prior.after_json != payload or prior.target_id != run_id:
                raise ValueError("conflicting control completion")
            return
        session.add(
            AdminOperationEventModel(
                actor_user_id=principal,
                actor_principal_kind="control",
                actor_principal_id=principal,
                control_request_id=request,
                trigger_message_id="",
                conversation_key="",
                capability="control.terminal.mutate",
                operation="control.terminal.completion",
                target_type="terminal",
                target_id=run_id,
                before_json="{}",
                after_json=payload,
                success=result["status"] == "succeeded",
                error_category=None if result["status"] == "succeeded" else str(result["status"]),
                duration_seconds=0,
                created_at=datetime.now(UTC),
            )
        )
