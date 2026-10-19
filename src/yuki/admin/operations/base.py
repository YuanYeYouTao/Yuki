"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.command_types import (
    CACHEABLE_COMMAND_FAILURES,
    ControlCommandError,
    bind_command_hash,
    failure_audit_target_type,
    success_audit_target_type,
)
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.json_types import JsonValue
from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.authority.problems import Problem, ProblemCode
from yuki.admin.control_resolution import USER_CAPABILITIES
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.identity import RequestId
from yuki.identity.authority import person_is_superuser
from yuki.persistence.command_receipts import (
    CommandReceiptRepository,
    CommandSuccess,
    pending_result,
)
from yuki.persistence.database import Database
from yuki.persistence.models import AdminOperationEventModel, ChatEventModel


def fail(code: ProblemCode) -> ControlCommandError:
    return ControlCommandError(Problem(code))


def authorize(principal: ControlPrincipal, capability: str) -> None:
    if not principal.allows(capability):
        raise fail(ProblemCode.CAPABILITY_DENIED)


async def authorize_current(
    database: Database,
    principal: ControlPrincipal,
    capability: str,
    *,
    session: AsyncSession | None = None,
) -> None:
    authorize(principal, capability)
    if (
        principal.source not in {PrincipalSource.QQ, PrincipalSource.PLATFORM}
        or capability in USER_CAPABILITIES
    ):
        return
    if session is None:
        async with database.sessions() as reader:
            allowed = await person_is_superuser(reader, principal.principal_id.text)
    else:
        allowed = await person_is_superuser(session, principal.principal_id.text)
    if not allowed:
        raise fail(ProblemCode.CAPABILITY_DENIED)


def revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise fail(ProblemCode.VERSION_CONFLICT)


def outcome(resource: str, rev: int, status: str, operation: str) -> CommandSuccess:
    state: dict[str, JsonValue] = {"resource": resource, "revision": rev, "status": status}
    return CommandSuccess(
        resource, rev, success_audit_target_type(operation), resource, {}, state, state
    )


class CommandExecutor:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.receipts = CommandReceiptRepository(database)

    async def prepare_chat_command(
        self,
        principal: ControlPrincipal,
        source_event_id: int,
        *,
        payload: Mapping[str, Any],
        revision: Callable[[], Awaitable[int]],
    ) -> ControlCommand:
        async with self.database.sessions() as session:
            event = await session.get(ChatEventModel, source_event_id)
            if event is None:
                raise fail(ProblemCode.NOT_FOUND)
            request_id = RequestId.parse(event.canonical_event_id)
            previous = await session.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request_id.text,
                    ControlCommandReceiptModel.principal_id == principal.principal_id.text,
                )
            )
        return ControlCommand(
            request_id=request_id,
            expected_revision=previous.expected_revision
            if previous is not None and previous.expected_revision is not None
            else await revision(),
            payload=payload,
            source_event_id=source_event_id,
        )

    async def _replay(
        self,
        session: AsyncSession,
        row: ControlCommandReceiptModel,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target: str,
        bound: str,
    ) -> ControlResult:
        if row.payload_hash != bound:
            raise fail(ProblemCode.IDEMPOTENCY_CONFLICT)
        if row.status in {"running", "unknown"}:
            audit = await session.get(AdminOperationEventModel, row.audit_id)
            if audit is None or audit.target_id != target:
                raise fail(ProblemCode.STATE_MISMATCH)
            self.receipts._require_audit_correlation(
                audit,
                principal=principal,
                command=command,
                operation=operation,
                capability=capability,
            )
            return pending_result(row, audit)
        result = await self.receipts.replay_receipt(
            session,
            row,
            bound,
            principal=principal,
            command=command,
            operation=operation,
            capability=capability,
            semantic_target_id=target,
            material=command.payload,
            failure_target_type=failure_audit_target_type(operation),
        )
        if isinstance(result, Problem):
            raise ControlCommandError(result)
        return result

    async def database_command(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target: str,
        mutate: Callable[[AsyncSession], Awaitable[CommandSuccess]],
    ) -> ControlResult:
        await authorize_current(self.database, principal, capability)
        started = monotonic()
        bound = bind_command_hash(
            operation=operation,
            target_id=target,
            expected_revision=command.expected_revision,
            payload=command.payload,
        )
        problem = None
        result = None
        async with self.database.immediate_session() as session:
            await authorize_current(self.database, principal, capability, session=session)
            old = await self.receipts.load_receipt(session, principal, command)
            if old is not None and command.source_event_id is not None:
                command = ControlCommand(
                    request_id=command.request_id,
                    expected_revision=old.expected_revision
                    if old.expected_revision is not None
                    else command.expected_revision,
                    payload=command.payload,
                    source_event_id=command.source_event_id,
                )
                bound = bind_command_hash(
                    operation=operation,
                    target_id=target,
                    expected_revision=command.expected_revision,
                    payload=command.payload,
                )
            if old is not None:
                return await self._replay(
                    session, old, principal, command, operation, capability, target, bound
                )
            try:
                async with session.begin_nested():
                    success = await mutate(session)
            except ControlCommandError as exc:
                if exc.problem.code not in CACHEABLE_COMMAND_FAILURES:
                    raise
                problem = await self.receipts.record_failure(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    target_type=failure_audit_target_type(operation),
                    target_id=target,
                    payload_hash=bound,
                    problem=exc.problem,
                    before={},
                    started=started,
                )
            else:
                result = await self.receipts.record_success(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    payload_hash=bound,
                    success=success,
                    semantic_target_id=target,
                    material=command.payload,
                    started=started,
                )
        if problem is not None:
            raise ControlCommandError(problem)
        assert result is not None
        return result

    async def external_command(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target: str,
        action: str,
        effect: Callable[[], Awaitable[CommandSuccess]],
    ) -> ControlResult:
        await authorize_current(self.database, principal, capability)
        started = monotonic()
        bound = bind_command_hash(
            operation=operation,
            target_id=target,
            expected_revision=command.expected_revision,
            payload=command.payload,
        )
        async with self.database.immediate_session() as session:
            await authorize_current(self.database, principal, capability, session=session)
            old = await self.receipts.load_receipt(session, principal, command)
            if old is not None and command.source_event_id is not None:
                command = ControlCommand(
                    request_id=command.request_id,
                    expected_revision=old.expected_revision
                    if old.expected_revision is not None
                    else command.expected_revision,
                    payload=command.payload,
                    source_event_id=command.source_event_id,
                )
                bound = bind_command_hash(
                    operation=operation,
                    target_id=target,
                    expected_revision=command.expected_revision,
                    payload=command.payload,
                )
            if old is not None:
                return await self._replay(
                    session, old, principal, command, operation, capability, target, bound
                )
            await self.receipts.reserve_external(
                session,
                principal,
                command,
                operation=operation,
                capability=capability,
                target_type=failure_audit_target_type(operation),
                target=target,
                payload_hash=bound,
                action=action,
            )
        try:
            await authorize_current(self.database, principal, capability)
            success = await effect()
            async with self.database.immediate_session() as session:
                reserved = await self.receipts.load_receipt(session, principal, command)
                return await self.receipts.record_success(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    payload_hash=bound,
                    success=success,
                    semantic_target_id=target,
                    material=command.payload,
                    started=started,
                    existing_receipt=reserved,
                )
        except BaseException as exc:
            if (
                isinstance(exc, ControlCommandError)
                and exc.problem.code in CACHEABLE_COMMAND_FAILURES
            ):
                async with self.database.immediate_session() as session:
                    reserved = await self.receipts.load_receipt(session, principal, command)
                    await self.receipts.record_failure(
                        session,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                        target_id=target,
                        target_type=failure_audit_target_type(operation),
                        payload_hash=bound,
                        problem=exc.problem,
                        before={},
                        started=started,
                        existing_receipt=reserved,
                    )
                raise
            # A committed result takes precedence when only its acknowledgement was lost.
            async with self.database.sessions() as session:
                old = await self.receipts.load_receipt(session, principal, command)
                if old is not None and old.status == "succeeded":
                    result = await self._replay(
                        session, old, principal, command, operation, capability, target, bound
                    )
                    if not isinstance(exc, Exception):
                        raise
                    return result
            result = await asyncio.shield(
                self.receipts.mark_unknown(
                    principal,
                    command,
                    operation=operation,
                    capability=capability,
                    interrupted=isinstance(exc, asyncio.CancelledError),
                    started=started,
                )
            )
            if not isinstance(exc, Exception):
                raise
            return result
