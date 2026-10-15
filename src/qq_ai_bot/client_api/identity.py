"""One-time possession proof and independent personal-device credentials."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.json_types import JsonValue
from qq_ai_bot.client_api.db_models import (
    APP_PLATFORM,
    ClientDeviceModel,
    ClientLinkCodeModel,
)
from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.conversation.canonical_db_models import (
    ControlCommandReceiptModel,
    PersonActiveRouteModel,
)
from qq_ai_bot.conversation.hydrate import conversation_for_binding, ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import find_identity_binding, find_presence
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.persistence.command_receipts import CommandReceiptRepository, aware
from qq_ai_bot.persistence.database import Database


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(operation: str, target: str, payload: object) -> str:
    return secret_hash(
        json.dumps([operation, target, payload], sort_keys=True, separators=(",", ":"))
    )


@dataclass(frozen=True, slots=True)
class ClientPrincipal:
    device_id: str
    person_id: str
    binding_id: str
    external_account_id: str
    conversation_id: str


class ClientIdentityService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.receipts = CommandReceiptRepository(database)

    async def authenticate(self, token: str) -> ClientPrincipal:
        async with self.database.sessions() as session, session.begin():
            device = await session.scalar(
                select(ClientDeviceModel).where(
                    ClientDeviceModel.token_hash == secret_hash(token),
                    ClientDeviceModel.revoked_at.is_(None),
                )
            )
            if device is None:
                raise ClientError("unauthenticated")
            principal = await self.require_device(session, device.id)
            device.last_used_at = datetime.now(UTC)
            return principal

    async def require_device(self, session: AsyncSession, device_id: str) -> ClientPrincipal:
        device = await session.get(ClientDeviceModel, device_id)
        if device is None or device.revoked_at is not None:
            raise ClientError("unauthenticated")
        person = await session.get(CanonicalPersonModel, device.person_id)
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.person_id == device.person_id,
                IdentityBindingModel.platform == APP_PLATFORM,
                IdentityBindingModel.status == "active",
            )
        )
        if person is None or not person.enabled or binding is None:
            raise ClientError("unauthenticated")
        conversation = await conversation_for_binding(session, binding.id)
        if conversation is None:
            raise ClientError("unauthenticated")
        return ClientPrincipal(
            device.id, person.id, binding.id, binding.external_account_id, conversation.id
        )

    async def replay(
        self, session: AsyncSession, principal_id: str, request_id: str, bound: str
    ) -> dict[str, Any] | None:
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
        assert row.status == "succeeded" and row.effective_state_json is not None
        return {**json.loads(row.effective_state_json), "replay": True}

    async def _person(self, session: AsyncSession, person_id: str) -> CanonicalPersonModel:
        person = await session.get(CanonicalPersonModel, person_id)
        if person is None or not person.enabled:
            raise ClientError("not_found")
        return person

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
        binding = IdentityBindingModel(
            id=str(uuid4()),
            person_id=person_id,
            platform=platform,
            external_account_id=account,
            display_name=display_name,
            status="active",
            revision=1,
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
            history_starts_at=now,
        )
        session.add(binding)
        await session.flush()
        await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key=f"binding:{binding.id}",
            identity_binding_id=binding.id,
            person_id=person_id,
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
    ) -> dict[str, Any]:
        await self._person(session, person_id)
        operation = "client.link_code"
        bound = fingerprint(operation, person_id, {"source_binding_id": source_binding_id})
        previous = await self.replay(session, issuer_id, request_id, bound)
        if previous is not None:
            return {**previous, "code": None}
        now = datetime.now(UTC)
        raw = secrets.token_urlsafe(24)
        code = ClientLinkCodeModel(
            id=str(uuid4()),
            code_hash=secret_hash(raw),
            person_id=person_id,
            issuer_principal_id=issuer_id,
            issuer_source=source,
            source_binding_id=source_binding_id,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        session.add(code)
        result = {"code_id": code.id, "expires_at": code.expires_at.isoformat()}
        await self.receipts.record_personal_success(
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
        )
        return {**result, "code": raw, "request_id": request_id, "replay": False}

    async def issue_for_device(self, principal: ClientPrincipal, request_id: str) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            current = await self.require_device(session, principal.device_id)
            return await self._issue_code(
                session,
                person_id=current.person_id,
                issuer_id=current.device_id,
                source="device",
                request_id=request_id,
                source_binding_id=current.binding_id,
            )

    async def issue_for_operator(
        self, person_id: str, principal_id: str, request_id: str
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            return await self._issue_code(
                session,
                person_id=person_id,
                issuer_id=principal_id,
                source="control",
                request_id=request_id,
            )

    async def _code(self, session: AsyncSession, raw: str, now: datetime) -> ClientLinkCodeModel:
        code = await session.scalar(
            select(ClientLinkCodeModel).where(
                ClientLinkCodeModel.code_hash == secret_hash(raw.strip())
            )
        )
        if code is None or code.used_at is not None or aware(code.expires_at) <= now:
            raise ClientError("unauthenticated")
        person = await session.get(CanonicalPersonModel, code.person_id)
        if person is None or not person.enabled:
            raise ClientError("unauthenticated")
        return code

    async def pair(self, raw_code: str, label: str) -> dict[str, str]:
        async with self.database.immediate_session() as session:
            now = datetime.now(UTC)
            code = await self._code(session, raw_code, now)
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.person_id == code.person_id,
                    IdentityBindingModel.platform == APP_PLATFORM,
                    IdentityBindingModel.status != "transferred",
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
            token = "yuki_device_" + secrets.token_urlsafe(32)
            device = ClientDeviceModel(
                id=str(uuid4()),
                person_id=code.person_id,
                token_hash=secret_hash(token),
                label=label,
                created_at=now,
            )
            session.add(device)
            code.used_at = now
            await self.receipts.record_personal_success(
                session,
                principal_id=device.id,
                principal_kind="device",
                person_id=code.person_id,
                request_id=str(uuid4()),
                operation="client.pair",
                operation_ref=device.id,
                resource_id=device.id,
                payload_hash=fingerprint("client.pair", code.id, label),
                result={"device_id": device.id},
            )
            return {"device_id": device.id, "token": token}

    async def transfer_binding(
        self, session: AsyncSession, binding: IdentityBindingModel, to_person: str, *, now: datetime
    ) -> IdentityBindingModel:
        await self._person(session, to_person)
        if binding.platform == APP_PLATFORM or binding.status != "active":
            raise ClientError("precondition_failed")
        if binding.person_id == to_person:
            return binding
        route = await session.get(PersonActiveRouteModel, binding.person_id)
        if route is not None and route.identity_binding_id == binding.id:
            route.paused = True
            route.route_generation += 1
            route.revision += 1
            route.updated_at = now
        binding.status = "transferred"
        binding.revision += 1
        binding.updated_at = now
        await session.flush()
        return await self._binding(
            session,
            person_id=to_person,
            platform=binding.platform,
            account=binding.external_account_id,
            display_name=binding.display_name,
            now=now,
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
        async with self.database.sessions() as session:
            principal = await self.require_device(session, principal.device_id)
            bindings = list(
                await session.scalars(
                    select(IdentityBindingModel)
                    .where(IdentityBindingModel.person_id == principal.person_id)
                    .order_by(IdentityBindingModel.created_at, IdentityBindingModel.id)
                )
            )
            devices = list(
                await session.scalars(
                    select(ClientDeviceModel)
                    .where(ClientDeviceModel.person_id == principal.person_id)
                    .order_by(ClientDeviceModel.created_at)
                )
            )
            return {
                "person_id": principal.person_id,
                "display_name": next(
                    (row.display_name for row in bindings if row.display_name), "本人"
                ),
                "conversation_id": principal.conversation_id,
                "bindings": [
                    {
                        "binding_id": row.id,
                        "platform": row.platform,
                        "display_name": row.display_name,
                        "status": row.status,
                    }
                    for row in bindings
                ],
                "devices": [
                    {
                        "device_id": row.id,
                        "label": row.label,
                        "created_at": row.created_at,
                        "last_used_at": row.last_used_at,
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
    ) -> dict[str, Any]:
        operation = "client.revoke"
        bound = fingerprint(operation, device.id, {})
        old = await self.replay(session, issuer_id, request_id, bound)
        if old is not None:
            return old
        if device.revoked_at is None:
            device.revoked_at = datetime.now(UTC)
        result: dict[str, JsonValue] = {"device_id": device.id, "revoked": True}
        await self.receipts.record_personal_success(
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
        )
        return {**result, "replay": False}

    async def revoke(
        self, principal: ClientPrincipal, device_id: str, request_id: str
    ) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            current = await self.require_device(session, principal.device_id)
            device = await session.get(ClientDeviceModel, device_id)
            if device is None or device.person_id != current.person_id:
                raise ClientError("not_found")
            return await self._revoke(
                session,
                device=device,
                issuer_id=current.device_id,
                source="device",
                request_id=request_id,
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
        self, binding_id: str, to_person: str, principal_id: str, request_id: str
    ) -> dict[str, Any]:
        operation = "identity.binding.transfer"
        bound = fingerprint(operation, binding_id, {"to_person": to_person})
        async with self.database.immediate_session() as session:
            previous = await self.replay(session, principal_id, request_id, bound)
            if previous is not None:
                return previous
            old = await session.get(IdentityBindingModel, binding_id)
            if old is None:
                raise ClientError("not_found")
            binding = await self.transfer_binding(session, old, to_person, now=datetime.now(UTC))
            conversation = await conversation_for_binding(session, binding.id)
            result: dict[str, JsonValue] = {
                "old_binding_id": old.id,
                "binding_id": binding.id,
                "person_id": binding.person_id,
                "conversation_id": conversation.id if conversation else None,
            }
            await self.receipts.record_personal_success(
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

    async def claim_account(
        self,
        raw_code: str,
        *,
        platform: str,
        account: str,
        display_name: str,
        message_id: str,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        if session is None:
            async with self.database.immediate_session() as writer:
                return await self.claim_account(
                    raw_code,
                    platform=platform,
                    account=account,
                    display_name=display_name,
                    message_id=message_id,
                    session=writer,
                )
        now = datetime.now(UTC)
        code = await self._code(session, raw_code, now)
        if platform == APP_PLATFORM or await find_presence(session, account, platform=platform):
            raise ClientError("precondition_failed")
        old = await find_identity_binding(session, account, platform=platform)
        if old is not None:
            binding = await self.transfer_binding(session, old, code.person_id, now=now)
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
            "person_id": binding.person_id,
            "transferred": old is not None and old.id != binding.id,
        }
        await self.receipts.record_personal_success(
            session,
            principal_id=binding.id,
            principal_kind="person",
            person_id=binding.person_id,
            request_id=str(uuid4()),
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
