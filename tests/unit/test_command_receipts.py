"""Interrupted intents keep their original IDs and never become retryable work."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from yuki.admin.authority.operations import OperationStatus
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.command_receipts import CommandReceiptRepository, control_operation
from yuki.persistence.models import AdminOperationEventModel


async def test_restart_recovers_original_intents_and_preserves_existing_outcomes(database):
    stamp = datetime.now(UTC) + timedelta(days=1)
    principal = str(uuid4())
    requests = {status: str(uuid4()) for status in ("running", "unknown", "failed")}
    async with database.immediate_session() as session:
        for status, request in requests.items():
            audit = AdminOperationEventModel(
                actor_user_id=principal,
                actor_principal_kind="control",
                actor_principal_id=principal,
                control_request_id=request,
                trigger_message_id="",
                conversation_key="",
                capability="control.plugin.mutate",
                operation="control.plugin.mutate",
                target_type="plugin",
                target_id="fixture",
                before_json="{}",
                after_json="{}",
                success=False,
                error_category="in_progress",
                duration_seconds=0,
                created_at=stamp,
            )
            session.add(audit)
            await session.flush()
            session.add(
                ControlCommandReceiptModel(
                    principal_id=principal,
                    request_id=request,
                    payload_hash="a" * 64,
                    status=status,
                    audit_id=audit.id,
                    problem_code=None if status == "running" else "original_error",
                    operation_kind="control",
                    operation_ref=f"control:{principal}:{request}",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )

    # A new repository represents a fresh process, with no executor or manager.
    assert await CommandReceiptRepository(database).recover_interrupted_controls() == 1
    assert await CommandReceiptRepository(database).recover_interrupted_controls() == 0
    async with database.read_session() as session:
        rows = {
            row.request_id: row for row in await session.scalars(select(ControlCommandReceiptModel))
        }
        recovered = control_operation(rows[requests["running"]])
        assert recovered.operation_id == f"control:{principal}:{requests['running']}"
        assert recovered.status is OperationStatus.UNKNOWN
        assert recovered.error_category == "process_restart"
        assert recovered.updated_at == stamp
        assert rows[requests["unknown"]].problem_code == "original_error"
        assert rows[requests["failed"]].status == "failed"
        assert rows[requests["failed"]].problem_code == "original_error"
