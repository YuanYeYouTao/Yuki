"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
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
from yuki.domain.identity import IdentityBindingId
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS, local_account_id
from yuki.identity.canonical_repository import (
    ensure_account,
    find_assignment,
    replace_assignment,
    set_assignment_usage,
)
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import current_person_id, same_person_id
from yuki.identity.routing import PresenceRouter
from yuki.persistence.command_receipts import (
    CommandSuccess,
)
from yuki.persistence.database import write_failure_committed
from yuki.persistence.people_repository import (
    PeopleRepository,
    PersonForgetPreparation,
    PrivacyPreparationChanged,
)
from yuki.persistence.unit_of_work import next_updated_at


class IdentityOperations:
    def __init__(
        self,
        executor: CommandExecutor,
        identities: ClientIdentityService,
        people: PeopleRepository,
        *,
        router: PresenceRouter,
    ) -> None:
        self.executor = executor
        self.database = executor.database
        self.receipts = executor.receipts
        self.identities = identities
        self.people = people
        self.router = router

    async def transfer_binding(
        self, principal: ControlPrincipal, binding_id: str, to_person: str, request_id: str
    ) -> dict[str, Any]:
        capability = "identity.binding.transfer"
        await authorize_current(self.database, principal, capability)
        async with self.database.read_session() as reader:
            previous = await self.identities.replay(
                reader,
                principal.principal_id.text,
                request_id,
                fingerprint(capability, binding_id, {"to_person": to_person}),
            )
        if previous is not None:
            return previous
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
        async with self.database.read_session() as reader:
            previous = await self.identities.replay(
                reader, principal.principal_id.text, request_id, bound
            )
        if previous is not None:
            return previous
        external = await self.people.prepare_person_forget_resources(
            person_id, operation_id=f"person-forget:{principal.principal_id.text}:{request_id}"
        )

        async def erase(session: AsyncSession, prepared: PersonForgetPreparation) -> dict[str, Any]:
            previous = await self.identities.replay(
                session, principal.principal_id.text, request_id, bound
            )
            if previous is not None:
                return previous
            await authorize_current(self.database, principal, operation, session=session)
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

        while True:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                prepared = await self.people.prepare_person_forget(
                    person_id, session=reader, closed_resources=external
                )
            if prepared is None:
                raise ClientError("not_found")
            try:
                async with self.database.immediate_session() as writer:
                    return await erase(writer, prepared)
            except PrivacyPreparationChanged as exc:
                if write_failure_committed(exc) is not False:
                    raise
                continue

    async def change_person(
        self,
        principal: ControlPrincipal,
        person_id: str,
        request_id: str,
        *,
        to_person: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        from yuki.identity.person_identity import (
            PersonMergePreparationChanged,
            merge_persons,
            prepare_person_merge,
            rename_person,
        )

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
                route_changes = await route_preparation(session)
                current = await merge_persons(
                    session,
                    person_id,
                    to_person,
                    actor_id=principal.principal_id.text,
                    prepared=prepared_merge,
                )
                route_changes()
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

        while True:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                previous = await self.identities.replay(
                    reader, principal.principal_id.text, request_id, bound
                )
                if previous is not None:
                    return previous
                if to_person is not None:
                    prepared_merge = await prepare_person_merge(reader, person_id, to_person)
                    route_preparation = await self.router.prepare_person_merge_routes(
                        reader, person_id, to_person
                    )
            try:
                async with self.database.immediate_session() as writer:
                    return await mutate(writer)
            except PersonMergePreparationChanged as exc:
                if write_failure_committed(exc) is not False:
                    raise
                continue

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
            existing = await find_assignment(
                session, parsed.external_account_id, platform=parsed.platform
            )
            if existing is not None and (
                existing.person_id is None
                or not await same_person_id(session, existing.person_id, person.id)
            ):
                raise fail(ProblemCode.BINDING_AMBIGUOUS)
            before: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            now = datetime.now(UTC)
            account = await ensure_account(
                session,
                parsed.external_account_id,
                platform=parsed.platform,
                now=now,
                display_name=parsed.display_name,
            )
            binding = await replace_assignment(
                session, account, existing.id if existing else None, person, now=now
            )
            assert isinstance(binding, IdentityBindingModel)
            if existing is None:
                person.revision += 1
                person.updated_at = next_updated_at(person.updated_at, now)
            state: dict[str, JsonValue] = {
                "binding_id": binding.id,
                "person_id": person.id,
                "platform": binding.platform,
                "status": binding.status,
                "revision": binding.revision,
            }
            return CommandSuccess(
                binding.id,
                binding.revision,
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
        return await self.binding_state(principal, identity, command, enabled=False)

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
            field = "is_superuser" if is_superuser is not None else "enabled"
            revision(
                original.revision if field == "enabled" else person.revision,
                command.expected_revision,
            )
            value = is_superuser if is_superuser is not None else enabled
            if value is None:
                raise fail(ProblemCode.VALIDATION_ERROR)
            if getattr(person, field) != value:
                setattr(person, field, value)
                person.revision += 1
                person.updated_at = next_updated_at(person.updated_at)
            if field == "enabled" and value is True and not original.enabled:
                original.enabled = True
                original.revision += 1
                original.updated_at = next_updated_at(original.updated_at)
            return outcome(
                identity,
                original.revision if field == "enabled" else person.revision,
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
        operation = (
            CommandOperation.BINDING_ENABLE if enabled else CommandOperation.BINDING_DISABLE
        ).value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            binding = await session.get(IdentityBindingModel, identity)
            if binding is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(binding.revision, command.expected_revision)
            if binding.status == "ended":
                if enabled:
                    raise fail(ProblemCode.PRECONDITION_FAILED)
            else:
                set_assignment_usage(binding, enabled, now=next_updated_at(binding.updated_at))
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
            if enabled is not None and row.status == "ended":
                raise fail(ProblemCode.PRECONDITION_FAILED)
            previous_revision = row.revision
            now = next_updated_at(row.updated_at)
            if enabled is not None:
                set_assignment_usage(row, enabled, now=now)
            if ingest_eligible is not None and row.ingest_eligible != ingest_eligible:
                row.ingest_eligible = ingest_eligible
                if row.revision == previous_revision:
                    row.revision += 1
                row.updated_at = now
            return outcome(
                identity, row.revision, "enabled" if row.enabled else "disabled", operation
            )

        return await self.executor.database_command(
            principal, command, operation, "identity.presence.set_ingest", identity, mutate
        )

    async def unbind_assignment(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand, *, kind: str
    ) -> ControlResult:
        model, operation = {
            "person": (IdentityBindingModel, CommandOperation.BINDING_UNBIND.value),
            "yuki": (PresenceModel, CommandOperation.PRESENCE_UNREGISTER.value),
        }[kind]
        ended_presence: str | None = None

        async def mutate(session: AsyncSession) -> CommandSuccess:
            nonlocal ended_presence
            stage = await session.get(model, identity)
            if stage is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(stage.revision, command.expected_revision)
            if stage.status != "ended":
                await replace_assignment(
                    session, stage.account, stage.id, None, now=datetime.now(UTC)
                )
            if isinstance(stage, PresenceModel):
                ended_presence = stage.id
            return outcome(stage.id, stage.revision, stage.status, operation)

        result = await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )
        if ended_presence is not None:
            self.router._registry.unbind_presence(ended_presence)
        return result

    async def register_presence(
        self, principal: ControlPrincipal, command: ControlCommand
    ) -> ControlResult:
        parsed = parse_attach_binding(
            {
                key: value
                for key, value in command.payload.items()
                if key != "expected_assignment_id"
            }
        )
        expected = command.payload.get("expected_assignment_id")
        if expected is not None and not isinstance(expected, str):
            raise fail(ProblemCode.VALIDATION_ERROR)
        expected = IdentityBindingId.parse(expected).text if expected is not None else None
        operation = CommandOperation.PRESENCE_REGISTER.value
        registered: tuple[str, str, str] | None = None

        async def mutate(session: AsyncSession) -> CommandSuccess:
            nonlocal registered
            if (
                parsed.platform in LOCAL_CLIENT_PLATFORMS
                and parsed.external_account_id != local_account_id(parsed.platform)
            ):
                raise fail(ProblemCode.PRECONDITION_FAILED)
            current = await find_assignment(
                session, parsed.external_account_id, platform=parsed.platform
            )
            if (current.id if current else None) != expected:
                raise fail(ProblemCode.BINDING_AMBIGUOUS)
            revision(current.revision if current else 0, command.expected_revision)
            now = datetime.now(UTC)
            account = await ensure_account(
                session,
                parsed.external_account_id,
                platform=parsed.platform,
                now=now,
                display_name=parsed.display_name,
            )
            presence = await replace_assignment(session, account, expected, "yuki", now=now)
            assert isinstance(presence, PresenceModel)
            registered = presence.platform, presence.external_account_id, presence.id
            return outcome(presence.id, presence.revision, presence.status, operation)

        result = await self.executor.database_command(
            principal, command, operation, operation, "yuki", mutate
        )
        if registered is not None:
            platform, account, presence_id = registered
            self.router._registry.bind_presence(
                platform=platform, external_account_id=account, presence_id=presence_id
            )
        return result

    async def transfer_presence(
        self, principal: ControlPrincipal, identity: str, to_person: str, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.PRESENCE_TRANSFER.value
        ended_presence: str | None = None

        async def mutate(session: AsyncSession) -> CommandSuccess:
            nonlocal ended_presence
            presence = await session.get(PresenceModel, identity)
            if presence is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(presence.revision, command.expected_revision)
            if presence.status == "ended" or presence.platform in LOCAL_CLIENT_PLATFORMS:
                raise fail(ProblemCode.PRECONDITION_FAILED)
            person = await session.get(
                CanonicalPersonModel, await current_person_id(session, to_person)
            )
            if person is None:
                raise fail(ProblemCode.NOT_FOUND)
            binding = await replace_assignment(
                session, presence.account, presence.id, person, now=datetime.now(UTC)
            )
            assert isinstance(binding, IdentityBindingModel)
            ended_presence = presence.id
            return outcome(binding.id, binding.revision, binding.status, operation)

        result = await self.executor.database_command(
            principal, command, operation, operation, identity, mutate
        )
        if ended_presence is not None:
            self.router._registry.unbind_presence(ended_presence)
        return result
