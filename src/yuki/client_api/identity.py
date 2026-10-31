"""One-time possession proof and independent personal-device credentials."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.json_types import JsonValue
from yuki.client_api.db_models import (
    ACP_PLATFORM,
    APP_PLATFORM,
    ClientDeviceModel,
    ClientLinkCodeModel,
)
from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
    PersonActiveRouteModel,
)
from yuki.conversation.hydrate import conversation_for_binding, ensure_canonical_conversation
from yuki.identity.authority import person_is_superuser
from yuki.identity.canonical_repository import (
    ensure_account,
    find_identity_binding,
    find_presence,
    replace_assignment,
)
from yuki.identity.credentials import (
    CredentialPurpose,
    DeviceCredentials,
    DevicePrincipal,
    require_person,
    secret_hash,
)
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import (
    PersonMergePreparation,
    PersonMergePreparationChanged,
    current_person_id,
    merge_persons,
    person_family_condition,
    prepare_person_merge,
    same_person_id,
)
from yuki.persistence.command_receipts import CommandReceiptRepository
from yuki.persistence.database import Database, write_failure_committed

if TYPE_CHECKING:
    from yuki.identity.routing import PresenceRouter


def fingerprint(operation: str, target: str, payload: object) -> str:
    return secret_hash(
        json.dumps([operation, target, payload], sort_keys=True, separators=(",", ":"))
    )


def request_message_key(
    external_account_id: str, request_id: str, *, conversation_id: str | None = None
) -> str:
    if conversation_id is not None:
        return f"{external_account_id}:{conversation_id}:{request_id}"
    return f"{external_account_id}:{request_id}"


@dataclass(frozen=True, slots=True)
class AccountClaimPreparation:
    code_id: str
    person_id: str
    old_binding: tuple[str, str] | None
    merge: PersonMergePreparation | None
    request_id: str


@dataclass(frozen=True, slots=True)
class ClientPrincipal:
    device_id: str
    person_id: str
    binding_id: str
    external_account_id: str
    conversation_id: str
    source_person_id: str
    request_person_id: str
    platform: str = APP_PLATFORM
    presence_id: str | None = None
    thread_id: str | None = None

    @property
    def request_conversation_id(self) -> str | None:
        return self.conversation_id if self.platform == ACP_PLATFORM else None

    @property
    def request_scope(self) -> str:
        return self.request_conversation_id or self.request_person_id

    def message_key(self, request_id: str) -> str:
        return request_message_key(
            self.external_account_id,
            request_id,
            conversation_id=self.request_conversation_id,
        )


class ClientIdentityService:
    def __init__(self, database: Database, router: PresenceRouter) -> None:
        self.database = database
        self.router = router
        self.receipts = CommandReceiptRepository(database)
        self.credentials = DeviceCredentials(database)

    async def authenticate(
        self,
        token: str,
        *,
        binding_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ClientPrincipal:
        async with self.database.read_session() as session:
            device = await self.credentials.authenticate(session, token, purpose="chat")
            return await self._chat_principal(
                session, device, binding_id=binding_id, conversation_id=conversation_id
            )

    async def require_device(
        self,
        session: AsyncSession,
        device_id: str,
        *,
        binding_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ClientPrincipal:
        device = await self.credentials.require_device(session, device_id, purpose="chat")
        return await self._chat_principal(
            session, device, binding_id=binding_id, conversation_id=conversation_id
        )

    async def _chat_principal(
        self,
        session: AsyncSession,
        device: DevicePrincipal,
        *,
        binding_id: str | None,
        conversation_id: str | None,
    ) -> ClientPrincipal:
        root_id = device.person_id
        if conversation_id is not None:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            binding = (
                await session.get(IdentityBindingModel, conversation.identity_binding_id)
                if conversation is not None and conversation.kind == "private"
                else None
            )
            presence = (
                await session.get(PresenceModel, conversation.entry_presence_id)
                if conversation is not None
                else None
            )
            if (
                conversation is None
                or binding is None
                or binding.platform != ACP_PLATFORM
                or (binding_id is not None and binding.id != binding_id)
                or presence is None
                or conversation.entry_presence_id != presence.id
                or not await same_person_id(session, binding.person_id, root_id)
                or not await same_person_id(session, cast(str, conversation.person_id), root_id)
            ):
                raise ClientError("not_found")
        else:
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.id == binding_id
                    if binding_id is not None
                    else IdentityBindingModel.person_id == device.source_person_id,
                    IdentityBindingModel.platform == APP_PLATFORM,
                )
            )
            if binding is None or not await same_person_id(session, binding.person_id, root_id):
                raise ClientError("unauthenticated")
            conversation = await conversation_for_binding(session, binding.id)
            if conversation is None:
                raise ClientError("unauthenticated")
            presence = await session.get(PresenceModel, conversation.entry_presence_id)
            if presence is None:
                raise ClientError("unavailable")
        return ClientPrincipal(
            device.device_id,
            root_id,
            binding.id,
            binding.external_account_id,
            conversation.id,
            device.source_person_id,
            binding.person_id,
            binding.platform,
            presence.id,
            conversation.thread_id,
        )

    async def require_principal(
        self, session: AsyncSession, principal: ClientPrincipal
    ) -> ClientPrincipal:
        return await self.require_device(
            session,
            principal.device_id,
            binding_id=principal.binding_id,
            conversation_id=principal.request_conversation_id,
        )

    async def require_superuser(
        self, session: AsyncSession, principal: ClientPrincipal
    ) -> ClientPrincipal:
        current = await self.require_principal(session, principal)
        if not await person_is_superuser(session, current.source_person_id):
            raise ClientError("forbidden")
        return current

    async def replay(
        self,
        session: AsyncSession,
        principal_id: str,
        request_id: str,
        bound: str,
        *,
        principal: ClientPrincipal | None = None,
    ) -> dict[str, Any] | None:
        device = await session.get(ClientDeviceModel, principal_id)
        if device is not None:
            from yuki.client_api.requests import ClientRequests, personal_command

            principal = principal or await self.require_device(session, device.id)
            row = await personal_command(
                session,
                principal.request_person_id,
                request_id,
                conversation_id=principal.request_conversation_id,
            )
            if row is None:
                if await ClientRequests(self).message(session, principal, request_id) is not None:
                    raise ClientError("request_conflict")
        else:
            row = await session.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.principal_id == principal_id,
                    ControlCommandReceiptModel.request_id == request_id,
                )
            )
        if row is None:
            return None
        if row.payload_hash != bound:
            raise ClientError("request_conflict")
        if row.status == "failed":
            from yuki.persistence.models import AdminOperationEventModel

            audit = await session.get(AdminOperationEventModel, row.audit_id)
            assert audit is not None and row.problem_code is not None
            raise ClientError(row.problem_code, details=json.loads(audit.after_json))
        assert row.status == "succeeded" and row.effective_state_json is not None
        return {**json.loads(row.effective_state_json), "replay": True}

    async def _binding(
        self,
        session: AsyncSession,
        *,
        person_id: str,
        platform: str,
        account: str,
        display_name: str,
        now: datetime,
    ) -> IdentityBindingModel:
        owner = await session.get(CanonicalPersonModel, person_id)
        if owner is None:
            raise ClientError("not_found")
        physical = await ensure_account(
            session, account, platform=platform, now=now, display_name=display_name
        )
        binding = cast(
            IdentityBindingModel, await replace_assignment(session, physical, None, owner, now=now)
        )
        if platform == APP_PLATFORM:
            presence = await find_presence(session, "yuki-app", platform=APP_PLATFORM)
            if presence is None:
                raise ClientError("unavailable")
            await ensure_canonical_conversation(
                session,
                kind="private",
                primary_scope_key=f"binding:{binding.id}",
                identity_binding_id=binding.id,
                person_id=person_id,
                presence_id=presence.id,
            )
        return binding

    async def _issue_code(
        self,
        session: AsyncSession,
        *,
        person_id: str,
        issuer_id: str,
        source: str,
        request_id: str,
        source_binding_id: str | None = None,
        trigger_message_id: str = "",
        principal: ClientPrincipal | None = None,
        purpose: CredentialPurpose = "chat",
    ) -> dict[str, Any]:
        try:
            await require_person(session, person_id)
        except ClientError as exc:
            raise ClientError("not_found") from exc
        operation = "client.link_code"
        payload: dict[str, str | None] = {"source_binding_id": source_binding_id}
        if purpose != "chat":
            payload["purpose"] = purpose
        bound = fingerprint(operation, person_id, payload)
        previous = await self.replay(session, issuer_id, request_id, bound, principal=principal)
        if previous is not None:
            return {**previous, "code": None}
        now = datetime.now(UTC)
        raw = secrets.token_urlsafe(24)
        code = ClientLinkCodeModel(
            id=str(uuid4()),
            code_hash=secret_hash(raw),
            purpose=purpose,
            person_id=person_id,
            issuer_principal_id=issuer_id,
            issuer_source=source,
            source_binding_id=source_binding_id,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        session.add(code)
        result = {
            "code_id": code.id,
            "expires_at": code.expires_at.isoformat(),
            "purpose": purpose,
        }
        await self.receipts.record_personal_command(
            session,
            principal_id=issuer_id,
            principal_kind=source if source in {"control", "device"} else "person",
            person_id=person_id,
            request_id=request_id,
            operation=operation,
            operation_ref=code.id,
            resource_id=code.id,
            payload_hash=bound,
            result=result,
            trigger_message_id=trigger_message_id,
            request_person_id=principal.request_person_id if principal is not None else None,
            request_conversation_id=(
                principal.request_conversation_id if principal is not None else None
            ),
        )
        return {**result, "code": raw, "request_id": request_id, "replay": False}

    async def issue_for_device(
        self,
        principal: ClientPrincipal,
        request_id: str,
        *,
        purpose: CredentialPurpose = "chat",
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            current = await self.require_principal(session, principal)
            return await self._issue_code(
                session,
                person_id=current.request_person_id,
                issuer_id=current.device_id,
                source="device",
                request_id=request_id,
                source_binding_id=current.binding_id,
                principal=current,
                purpose=purpose,
            )

    async def issue_for_operator(
        self,
        person_id: str,
        principal_id: str,
        request_id: str,
        *,
        purpose: CredentialPurpose = "chat",
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            return await self._issue_code(
                session,
                person_id=person_id,
                issuer_id=principal_id,
                source="control",
                request_id=request_id,
                purpose=purpose,
            )

    async def _code(self, session: AsyncSession, raw: str, now: datetime) -> ClientLinkCodeModel:
        return await self.credentials.code(session, raw, now, purpose="chat")

    async def pair(self, raw_code: str, label: str) -> dict[str, str]:
        async with self.database.immediate_session() as session:
            now = datetime.now(UTC)
            code = await self._code(session, raw_code, now)
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.person_id == code.person_id,
                    IdentityBindingModel.platform == APP_PLATFORM,
                    IdentityBindingModel.status != "ended",
                )
            )
            if binding is None:
                binding = await self._binding(
                    session,
                    person_id=code.person_id,
                    platform=APP_PLATFORM,
                    account=str(uuid4()),
                    display_name="",
                    now=now,
                )
            if binding.status != "active":
                raise ClientError("precondition_failed")
            return await self.credentials.pair(session, code, label, now=now)

    async def transfer_binding(
        self, session: AsyncSession, binding: IdentityBindingModel, to_person: str, *, now: datetime
    ) -> IdentityBindingModel:
        target = await session.get(
            CanonicalPersonModel, await current_person_id(session, to_person)
        )
        if target is None:
            raise ClientError("not_found")
        if binding.platform in {APP_PLATFORM, ACP_PLATFORM} or binding.status == "ended":
            raise ClientError("precondition_failed")
        if await same_person_id(session, binding.person_id, target.id):
            return binding
        route = await session.get(
            PersonActiveRouteModel, await current_person_id(session, binding.person_id)
        )
        if route is not None and route.identity_binding_id == binding.id:
            route.paused = True
            route.route_generation += 1
            route.revision += 1
            route.updated_at = now
        return cast(
            IdentityBindingModel,
            await replace_assignment(session, binding.account, binding.id, target, now=now),
        )

    async def cleanup_expired(self) -> int:
        async with self.database.immediate_session() as session:
            result = await session.execute(
                delete(ClientLinkCodeModel).where(
                    ClientLinkCodeModel.expires_at <= datetime.now(UTC)
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    async def me(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.database.read_session() as session:
            principal = await self.require_principal(session, principal)
            bindings = list(
                await session.scalars(
                    select(IdentityBindingModel)
                    .where(
                        person_family_condition(IdentityBindingModel.person_id, principal.person_id)
                    )
                    .order_by(IdentityBindingModel.created_at, IdentityBindingModel.id)
                )
            )
            devices = list(
                await session.scalars(
                    select(ClientDeviceModel)
                    .where(
                        person_family_condition(ClientDeviceModel.person_id, principal.person_id)
                    )
                    .order_by(ClientDeviceModel.created_at)
                )
            )
            person = await session.get(CanonicalPersonModel, principal.person_id)
            assert person is not None
            return {
                "person_id": principal.person_id,
                "is_superuser": await person_is_superuser(session, principal.source_person_id),
                "source_person_id": principal.source_person_id,
                "request_person_id": principal.request_person_id,
                "request_conversation_id": principal.request_conversation_id,
                "request_scope": principal.request_scope,
                "platform": principal.platform,
                "presence_id": principal.presence_id,
                "thread_id": principal.thread_id,
                "binding_id": principal.binding_id,
                "display_name": person.display_name or "本人",
                "conversation_id": principal.conversation_id,
                "bindings": [
                    {
                        "binding_id": row.id,
                        "platform": row.platform,
                        "display_name": row.display_name,
                        "status": row.status,
                        "source_person_id": row.person_id,
                    }
                    for row in bindings
                ],
                "devices": [
                    {
                        "device_id": row.id,
                        "purpose": row.purpose,
                        "label": row.label,
                        "created_at": row.created_at,
                        "revoked_at": row.revoked_at,
                    }
                    for row in devices
                ],
            }

    async def _revoke(
        self,
        session: AsyncSession,
        *,
        device: ClientDeviceModel,
        issuer_id: str,
        source: str,
        request_id: str,
        principal: ClientPrincipal | None = None,
    ) -> dict[str, Any]:
        operation = "client.revoke"
        bound = fingerprint(operation, device.id, {})
        old = await self.replay(session, issuer_id, request_id, bound, principal=principal)
        if old is not None:
            return old
        if device.revoked_at is None:
            device.revoked_at = datetime.now(UTC)
        result: dict[str, JsonValue] = {"device_id": device.id, "revoked": True}
        await self.receipts.record_personal_command(
            session,
            principal_id=issuer_id,
            principal_kind=source,
            person_id=device.person_id,
            request_id=request_id,
            operation=operation,
            operation_ref=device.id,
            payload_hash=bound,
            resource_id=device.id,
            result=result,
            request_person_id=principal.request_person_id if principal is not None else None,
            request_conversation_id=(
                principal.request_conversation_id if principal is not None else None
            ),
        )
        return {**result, "replay": False}

    async def revoke(
        self, principal: ClientPrincipal, device_id: str, request_id: str
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            current = await self.require_principal(session, principal)
            device = await session.get(ClientDeviceModel, device_id)
            if device is None or not await same_person_id(
                session, device.person_id, current.person_id
            ):
                raise ClientError("not_found")
            return await self._revoke(
                session,
                device=device,
                issuer_id=current.device_id,
                source="device",
                request_id=request_id,
                principal=current,
            )

    async def revoke_for_operator(
        self, device_id: str, principal_id: str, request_id: str
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            device = await session.get(ClientDeviceModel, device_id)
            if device is None:
                raise ClientError("not_found")
            return await self._revoke(
                session,
                device=device,
                issuer_id=principal_id,
                source="control",
                request_id=request_id,
            )

    async def transfer_for_operator(
        self,
        binding_id: str,
        to_person: str,
        principal_id: str,
        request_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        if session is None:
            bound = fingerprint("identity.binding.transfer", binding_id, {"to_person": to_person})
            async with self.database.read_session() as reader:
                previous = await self.replay(reader, principal_id, request_id, bound)
            if previous is not None:
                return previous
            async with self.database.immediate_session() as writer:
                return await self.transfer_for_operator(
                    binding_id, to_person, principal_id, request_id, session=writer
                )
        operation = "identity.binding.transfer"
        bound = fingerprint(operation, binding_id, {"to_person": to_person})
        previous = await self.replay(session, principal_id, request_id, bound)
        if previous is not None:
            return previous
        old = await session.get(IdentityBindingModel, binding_id)
        if old is None:
            raise ClientError("not_found")
        binding = await self.transfer_binding(session, old, to_person, now=datetime.now(UTC))
        result: dict[str, JsonValue] = {
            "old_binding_id": old.id,
            "binding_id": binding.id,
            "person_id": binding.person_id,
        }
        await self.receipts.record_personal_command(
            session,
            principal_id=principal_id,
            principal_kind="control",
            person_id=to_person,
            request_id=request_id,
            operation=operation,
            operation_ref=binding.id,
            payload_hash=bound,
            resource_id=binding.id,
            result=result,
        )
        return {**result, "replay": False}

    async def issue_for_account(
        self, *, platform: str, account: str, message_id: str, session: AsyncSession | None = None
    ) -> dict[str, Any]:
        if session is None:
            async with self.database.immediate_session() as writer:
                return await self.issue_for_account(
                    platform=platform, account=account, message_id=message_id, session=writer
                )
        binding = await find_identity_binding(session, account, platform=platform)
        if binding is None or binding.status != "active":
            raise ClientError("unauthenticated")
        return await self._issue_code(
            session,
            person_id=binding.person_id,
            issuer_id=binding.id,
            source=platform,
            request_id=str(uuid4()),
            source_binding_id=binding.id,
            trigger_message_id=message_id,
        )

    async def prepare_account_claim(
        self,
        raw_code: str,
        *,
        platform: str,
        account: str,
        session: AsyncSession | None = None,
        request_id: str | None = None,
    ) -> AccountClaimPreparation:
        if session is None:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                return await self.prepare_account_claim(
                    raw_code,
                    platform=platform,
                    account=account,
                    session=reader,
                    request_id=request_id,
                )
        code = await self._code(session, raw_code, datetime.now(UTC))
        presence = await find_presence(session, account, platform=platform)
        if platform in {APP_PLATFORM, ACP_PLATFORM} or presence is not None:
            raise ClientError("precondition_failed")
        old = await find_identity_binding(session, account, platform=platform)
        return AccountClaimPreparation(
            code_id=code.id,
            person_id=code.person_id,
            old_binding=(old.id, old.person_id) if old is not None else None,
            merge=await prepare_person_merge(session, old.person_id, code.person_id)
            if old is not None
            else None,
            request_id=request_id or str(uuid4()),
        )

    async def claim_account(
        self,
        raw_code: str,
        *,
        platform: str,
        account: str,
        display_name: str,
        message_id: str,
        session: AsyncSession | None = None,
        prepared: AccountClaimPreparation | None = None,
    ) -> dict[str, Any]:
        if session is None:
            request_id = str(uuid4())
            while True:
                prepared = await self.prepare_account_claim(
                    raw_code, platform=platform, account=account, request_id=request_id
                )
                try:
                    async with self.database.immediate_session() as writer:
                        return await self.claim_account(
                            raw_code,
                            platform=platform,
                            account=account,
                            display_name=display_name,
                            message_id=message_id,
                            session=writer,
                            prepared=prepared,
                        )
                except PersonMergePreparationChanged as exc:
                    if write_failure_committed(exc) is not False:
                        raise
                    continue
        if prepared is None:
            raise ValueError("account_claim_preparation_required")
        now = datetime.now(UTC)
        code = await self._code(session, raw_code, now)
        presence = await find_presence(session, account, platform=platform)
        if platform in {APP_PLATFORM, ACP_PLATFORM} or presence is not None:
            raise ClientError("precondition_failed")
        old = await find_identity_binding(session, account, platform=platform)
        if (code.id, code.person_id, (old.id, old.person_id) if old else None) != (
            prepared.code_id,
            prepared.person_id,
            prepared.old_binding,
        ):
            raise PersonMergePreparationChanged()
        merged = old is not None and not await same_person_id(
            session, old.person_id, code.person_id
        )
        if old is not None:
            route_preparation = await self.router.prepare_person_merge_routes(
                session, old.person_id, code.person_id
            )
            apply_routes = await route_preparation(session)
            await merge_persons(
                session,
                old.person_id,
                code.person_id,
                now=now,
                actor_id=code.issuer_principal_id,
                prepared=cast(PersonMergePreparation, prepared.merge),
            )
            apply_routes()
            binding = old
        else:
            binding = await self._binding(
                session,
                person_id=code.person_id,
                platform=platform,
                account=account,
                display_name=display_name,
                now=now,
            )
        code.used_at = now
        result: dict[str, JsonValue] = {
            "binding_id": binding.id,
            "person_id": await current_person_id(session, binding.person_id),
            "ended": False,
            "merged": merged,
        }
        await self.receipts.record_personal_command(
            session,
            principal_id=binding.id,
            principal_kind="person",
            person_id=binding.person_id,
            request_id=prepared.request_id,
            operation="client.binding_link",
            operation_ref=binding.id,
            payload_hash=fingerprint(
                "client.binding_link", code.id, {"platform": platform, "account": account}
            ),
            resource_id=binding.id,
            result=result,
            trigger_message_id=message_id,
        )
        return result
