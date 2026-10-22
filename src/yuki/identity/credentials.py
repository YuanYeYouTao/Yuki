"""Device possession and current Person authority, without a chat entry."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.db_models import ClientDeviceModel, ClientLinkCodeModel
from yuki.client_api.errors import ClientError
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.person_identity import current_person_id, same_person_id
from yuki.persistence.command_receipts import CommandReceiptRepository, aware
from yuki.persistence.database import Database

CredentialPurpose = Literal["chat", "connector"]


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DevicePrincipal:
    device_id: str
    person_id: str
    source_person_id: str


async def require_person(session: AsyncSession, source_person_id: str) -> str:
    source = await session.get(CanonicalPersonModel, source_person_id)
    if source is None or not source.enabled:
        raise ClientError("unauthenticated")
    root_id = await current_person_id(session, source.id)
    root = await session.get(CanonicalPersonModel, root_id)
    if root is None or not root.enabled:
        raise ClientError("unauthenticated")
    return root.id


class DeviceCredentials:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.receipts = CommandReceiptRepository(database)

    async def authenticate(
        self, session: AsyncSession, token: str, *, purpose: CredentialPurpose
    ) -> DevicePrincipal:
        device = await session.scalar(
            select(ClientDeviceModel).where(
                ClientDeviceModel.token_hash == secret_hash(token),
                ClientDeviceModel.purpose == purpose,
                ClientDeviceModel.revoked_at.is_(None),
            )
        )
        if device is None:
            raise ClientError("unauthenticated")
        return await self.require_device(session, device.id, purpose=purpose)

    async def require_device(
        self, session: AsyncSession, device_id: str, *, purpose: CredentialPurpose
    ) -> DevicePrincipal:
        device = await session.get(ClientDeviceModel, device_id)
        if device is None or device.revoked_at is not None or device.purpose != purpose:
            raise ClientError("unauthenticated")
        person_id = await require_person(session, device.person_id)
        return DevicePrincipal(device.id, person_id, device.person_id)

    async def code(
        self,
        session: AsyncSession,
        raw: str,
        now: datetime,
        *,
        purpose: CredentialPurpose,
    ) -> ClientLinkCodeModel:
        code = await session.scalar(
            select(ClientLinkCodeModel).where(
                ClientLinkCodeModel.code_hash == secret_hash(raw.strip()),
                ClientLinkCodeModel.purpose == purpose,
            )
        )
        if code is None or code.used_at is not None or aware(code.expires_at) <= now:
            raise ClientError("unauthenticated")
        if code.source_binding_id is not None:
            source = await session.get(IdentityBindingModel, code.source_binding_id)
            if (
                source is None
                or source.status != "active"
                or not await same_person_id(session, source.person_id, code.person_id)
            ):
                raise ClientError("unauthenticated")
        await require_person(session, code.person_id)
        return code

    async def pair(
        self,
        session: AsyncSession,
        code: ClientLinkCodeModel,
        label: str,
        *,
        now: datetime,
    ) -> dict[str, str]:
        token = "yuki_device_" + secrets.token_urlsafe(32)
        device = ClientDeviceModel(
            id=str(uuid4()),
            person_id=code.person_id,
            purpose=code.purpose,
            token_hash=secret_hash(token),
            label=label,
            created_at=now,
        )
        session.add(device)
        code.used_at = now
        operation = "connector.pair" if code.purpose == "connector" else "client.pair"
        await self.receipts.record_personal_command(
            session,
            principal_id=device.id,
            principal_kind="device",
            person_id=code.person_id,
            request_id=str(uuid4()),
            operation=operation,
            operation_ref=device.id,
            resource_id=device.id,
            payload_hash=secret_hash(f"{operation}:{code.id}:{label}"),
            result={"device_id": device.id, "purpose": code.purpose},
        )
        return {"device_id": device.id, "token": token}
