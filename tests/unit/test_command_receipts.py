"""Interrupted intents keep their original IDs and never become retryable work."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.admin.authority.command_types import ControlCommandError
from qq_ai_bot.admin.authority.commands import ControlCommand
from qq_ai_bot.admin.authority.operations import OperationStatus
from qq_ai_bot.admin.authority.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.admin.authority.problems import ProblemCode
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.persistence.command_receipts import CommandReceiptRepository, control_operation
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.models import AdminOperationEventModel


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
    async with database.sessions() as session:
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


async def test_unknown_external_effect_replays_original_receipt_without_second_dispatch(
    database, monkeypatch
):
    adapter = ControlCommandAdapter(database)
    principal = ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=None,
        source=PrincipalSource.CLI,
        authenticated=True,
        active=True,
    )
    command = ControlCommand(
        request_id=RequestId.new(),
        expected_revision=1,
        payload={"resource_id": "fixture", "action": "enable"},
    )
    dispatched = []

    async def validate(*args):
        return None

    async def effect(*args, **kwargs):
        # The durable intent has committed and no SQLite writer spans the effect.
        async with database.immediate_session() as session:
            row = await adapter._receipts.load_receipt(session, principal, command)
            assert row.status == "running"
        dispatched.append(command.request_id.text)
        raise RuntimeError("effect happened; acknowledgement lost")

    monkeypatch.setattr(adapter._management, "prepare_external", lambda *args: None)
    monkeypatch.setattr(adapter._management, "validate_external", validate)
    monkeypatch.setattr(adapter._management, "execute_external", effect)
    original = await adapter.mutate_plugin(principal, "fixture", command)
    assert original.operation.status is OperationStatus.UNKNOWN
    assert await adapter.mutate_plugin(principal, "fixture", command) == original
    assert dispatched == [command.request_id.text]
    async with database.sessions() as session:
        row = await adapter._receipts.load_receipt(session, principal, command)
        assert row.status == "unknown"
        assert row.operation_ref == original.operation.operation_id
    another = ControlCommand(
        request_id=RequestId.new(), expected_revision=1, payload=command.payload
    )
    with pytest.raises(ControlCommandError) as exc:
        await adapter.mutate_plugin(principal, "fixture", another)
    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
    assert dispatched == [command.request_id.text]
