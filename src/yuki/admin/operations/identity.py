"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.command_types import (
    CommandOperation,
    parse_attach_binding,
)
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.json_types import JsonValue
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.operations.base import CommandExecutor, authorize_current, fail, outcome, revision
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, fingerprint
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import current_person_id, same_person_id
from yuki.persistence.command_receipts import (
    CommandSuccess,
)
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.unit_of_work import next_updated_at


class IdentityOperations:
    def __init__(
        self, executor: CommandExecutor, identities: ClientIdentityService, people: PeopleRepository
    ) -> None:
        self.executor = executor
        self.database = executor.database
        self.receipts = executor.receipts
        self.identities = identities
        self.people = people

    async def transfer_binding(
        self, principal: ControlPrincipal, binding_id: str, to_person: str, request_id: str
    ) -> dict[str, Any]:
        capability = "identity.binding.transfer"
        await authorize_current(self.database, principal, capability)
        async with self.database.immediate_session() as session:
            await authorize_current(self.database, principal, capability, session=session)
            return await self.identities.transfer_for_operator(
                binding_id, to_person, principal.principal_id.text, request_id, session=session
            )

    async def forget_person(
        self, principal: ControlPrincipal, person_id: str, request_id: str
    ) -> dict[str, Any]:
        await authorize_current(self.database, principal, "identity.person.forget")
        operation = "identity.person.forget"
        bound = fingerprint(operation, person_id, {})
        async with self.database.sessions() as reader:
            previous = await self.identities.replay(
                reader, principal.principal_id.text, request_id, bound
            )
        if previous is not None:
            return previous
        external = await self.people.prepare_person_forget_resources(
            person_id, operation_id=f"person-forget:{principal.principal_id.text}:{request_id}"
        )

        async def erase(session: AsyncSession) -> dict[str, Any]:
            previous = await self.identities.replay(
                session, principal.principal_id.text, request_id, bound
            )
            if previous is not None:
                return previous
            await authorize_current(self.database, principal, operation, session=session)
            prepared = await self.people.prepare_person_forget(
                person_id, session=session, closed_resources=external
            )
            if prepared is None:
                raise ClientError("not_found")
            await self.people.apply_person_forget(session, prepared)
            result: dict[str, JsonValue] = {"person_id": person_id, "deleted": True}
            if prepared.peer_cleanup_results is not None:
                result["peer_cleanup"] = prepared.peer_cleanup_results
            await self.receipts.record_personal_command(
                session,
                principal_id=principal.principal_id.text,
                principal_kind="control",
                person_id=person_id,
                request_id=request_id,
                operation=operation,
                operation_ref=person_id,
                payload_hash=bound,
                resource_id=person_id,
                result=result,
            )
            return result

        return await self.database.apply_snapshot_write(erase)

    async def change_person(
        self,
        principal: ControlPrincipal,
        person_id: str,
        request_id: str,
        *,
        to_person: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        from yuki.identity.person_identity import merge_persons, rename_person

        operation = "identity.person.merge" if to_person is not None else "identity.person.rename"
        await authorize_current(self.database, principal, operation)
        bound = fingerprint(
            operation, person_id, {"to_person": to_person, "display_name": display_name}
        )

        async def mutate(session: AsyncSession) -> dict[str, Any]:
            previous = await self.identities.replay(
                session, principal.principal_id.text, request_id, bound
            )
            if previous is not None:
                return previous
            await authorize_current(self.database, principal, operation, session=session)
            if to_person is not None:
                current = await merge_persons(
                    session,
                    person_id,
                    to_person,
                    actor_id=principal.principal_id.text,
                )
            else:
                assert display_name is not None
                row = await rename_person(session, person_id, display_name, now=datetime.now(UTC))
                current = row.id
            result: dict[str, JsonValue] = {"person_id": current}
            await self.receipts.record_personal_command(
                session,
                principal_id=principal.principal_id.text,
                principal_kind="control",
                person_id=current,
                request_id=request_id,
                operation=operation,
                operation_ref=current,
                payload_hash=bound,
                resource_id=current,
                result=result,
            )
            return {**result, "replay": False}

        return await self.database.apply_snapshot_write(mutate)

    async def create_person(
        self, principal: ControlPrincipal, command: ControlCommand
    ) -> ControlResult:
        from uuid import uuid4

        operation = CommandOperation.PERSON_CREATE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            revision(0, command.expected_revision)
            now = datetime.now(UTC)
            person = CanonicalPersonModel(
                id=str(uuid4()),
                display_name=str(command.payload.get("display_name", "")).strip(),
                created_at=now,
                updated_at=now,
            )
            session.add(person)
            await session.flush()
            return outcome(person.id, person.revision, "created", operation)

        return await self.executor.database_command(
            principal, command, operation, operation, "yuki", mutate
        )

    async def disable_person(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.PERSON_DISABLE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            original = await session.get(CanonicalPersonModel, identity)
            if original is None:
                raise fail(ProblemCode.NOT_FOUND)
            person = await session.get(
                CanonicalPersonModel, await current_person_id(session, identity)
            )
            assert person is not None
            revision(person.revision, command.expected_revision)
            before: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            if person.enabled:
                person.enabled = False
                person.revision += 1
                person.updated_at = next_updated_at(person.updated_at)
            after: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            return CommandSuccess(
                identity, person.revision, "person", identity, before, after, after
            )

        return await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def attach_binding(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand
    ) -> ControlResult:
        from uuid import uuid4

        operation = CommandOperation.BINDING_ATTACH.value
        parsed = parse_attach_binding(command.payload)
        command = ControlCommand(
            request_id=command.request_id,
            expected_revision=command.expected_revision,
            source_event_id=command.source_event_id,
            payload={
                "platform": parsed.platform,
                "external_account_id": parsed.external_account_id,
                "display_name": parsed.display_name,
            },
        )

        async def mutate(session: AsyncSession) -> CommandSuccess:
            original = await session.get(CanonicalPersonModel, identity)
            if original is None:
                raise fail(ProblemCode.NOT_FOUND)
            person = await session.get(
                CanonicalPersonModel, await current_person_id(session, identity)
            )
            assert person is not None
            revision(person.revision, command.expected_revision)
            if parsed.platform in LOCAL_CLIENT_PLATFORMS:
                # The first-party account ID is generated by device pairing.
                raise fail(ProblemCode.PRECONDITION_FAILED)
            existing = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.platform == parsed.platform,
                    IdentityBindingModel.external_account_id == parsed.external_account_id,
                    IdentityBindingModel.status != "transferred",
                )
            )
            presence = await session.scalar(
                select(PresenceModel.id).where(
                    PresenceModel.platform == parsed.platform,
                    PresenceModel.external_account_id == parsed.external_account_id,
                    PresenceModel.enabled.is_(True),
                )
            )
            if existing is not None:
                raise fail(
                    ProblemCode.PRECONDITION_FAILED
                    if await same_person_id(session, existing.person_id, person.id)
                    else ProblemCode.BINDING_AMBIGUOUS
                )
            if presence is not None:
                raise fail(ProblemCode.PRECONDITION_FAILED)
            before: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            now = datetime.now(UTC)
            binding = IdentityBindingModel(
                id=str(uuid4()),
                person_id=person.id,
                platform=parsed.platform,
                external_account_id=parsed.external_account_id,
                display_name=parsed.display_name,
                status="active",
                revision=1,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
                history_starts_at=now,
            )
            session.add(binding)
            person.revision += 1
            person.updated_at = next_updated_at(person.updated_at, now)
            state: dict[str, JsonValue] = {
                "binding_id": binding.id,
                "person_id": person.id,
                "platform": binding.platform,
                "status": binding.status,
                "revision": 1,
            }
            return CommandSuccess(
                binding.id,
                1,
                "identity_binding",
                binding.id,
                before,
                {**state, "owner_revision": person.revision},
                state,
            )

        return await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def disable_binding(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.BINDING_DISABLE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            binding = await session.get(IdentityBindingModel, identity)
            if binding is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(binding.revision, command.expected_revision)
            if binding.status == "transferred":
                return outcome(identity, binding.revision, binding.status, operation)
            if binding.status != "disabled":
                binding.status = "disabled"
                binding.revision += 1
                binding.updated_at = next_updated_at(binding.updated_at)
            return outcome(identity, binding.revision, "disabled", operation)

        return await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def person_state(
        self,
        principal: ControlPrincipal,
        identity: str,
        command: ControlCommand,
        *,
        enabled: bool | None = None,
        is_superuser: bool | None = None,
    ) -> ControlResult:
        if enabled is False and is_superuser is None:
            return await self.disable_person(principal, identity, command)
        operation = (
            CommandOperation.PERSON_SUPERUSER.value
            if is_superuser is not None
            else CommandOperation.PERSON_ENABLE.value
        )

        async def mutate(session: AsyncSession) -> CommandSuccess:
            original = await session.get(CanonicalPersonModel, identity)
            if original is None:
                raise fail(ProblemCode.NOT_FOUND)
            person = await session.get(
                CanonicalPersonModel, await current_person_id(session, identity)
            )
            assert person is not None
            revision(person.revision, command.expected_revision)
            field = "is_superuser" if is_superuser is not None else "enabled"
            value = is_superuser if is_superuser is not None else enabled
            if value is None:
                raise fail(ProblemCode.VALIDATION_ERROR)
            if getattr(person, field) != value:
                setattr(person, field, value)
                person.revision += 1
                person.updated_at = next_updated_at(person.updated_at)
            return outcome(
                identity,
                person.revision,
                ("superuser" if value else "revoked")
                if field == "is_superuser"
                else ("enabled" if value else "disabled"),
                operation,
            )

        return await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def binding_state(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand, *, enabled: bool
    ) -> ControlResult:
        if not enabled:
            return await self.disable_binding(principal, identity, command)
        operation = CommandOperation.BINDING_ENABLE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            binding = await session.get(IdentityBindingModel, identity)
            if binding is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(binding.revision, command.expected_revision)
            if binding.status == "transferred":
                raise fail(ProblemCode.PRECONDITION_FAILED)
            if binding.status != "active":
                binding.status = "active"
                binding.revision += 1
                binding.updated_at = next_updated_at(binding.updated_at)
            return outcome(identity, binding.revision, binding.status, operation)

        return await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def presence_state(
        self,
        principal: ControlPrincipal,
        identity: str,
        command: ControlCommand,
        *,
        enabled: bool | None = None,
        ingest_eligible: bool | None = None,
    ) -> ControlResult:
        operation = CommandOperation.PRESENCE_STATE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            row = await session.get(PresenceModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(row.revision, command.expected_revision)
            changed = False
            for field, value in (("enabled", enabled), ("ingest_eligible", ingest_eligible)):
                if value is not None and getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
            if changed:
                row.revision += 1
                row.updated_at = next_updated_at(row.updated_at)
            return outcome(
                identity, row.revision, "enabled" if row.enabled else "disabled", operation
            )

        return await self.executor.database_command(
            principal, command, operation, "identity.presence.set_ingest", identity, mutate
        )
