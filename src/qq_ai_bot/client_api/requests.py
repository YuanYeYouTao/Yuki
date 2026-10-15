"""Authorized projections of original event and command receipts across devices."""

import json
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.client_api.db_models import ClientDeviceModel
from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientIdentityService, ClientPrincipal
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalEventReceiptModel,
    ControlCommandReceiptModel,
)
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.persistence.models import ChatEventModel


async def personal_command(
    session: AsyncSession, person_id: str, request_id: str
) -> ControlCommandReceiptModel | None:
    return cast(
        ControlCommandReceiptModel | None,
        await session.scalar(
            select(ControlCommandReceiptModel)
            .join(
                ClientDeviceModel, ClientDeviceModel.id == ControlCommandReceiptModel.principal_id
            )
            .where(
                ClientDeviceModel.person_id == person_id,
                ControlCommandReceiptModel.request_id == request_id,
            )
        ),
    )


class ClientRequests:
    def __init__(self, identity: ClientIdentityService) -> None:
        self.identity = identity

    async def message(
        self, session: AsyncSession, principal: ClientPrincipal, request_id: str
    ) -> ChatEventModel | None:
        presence = await session.scalar(
            select(PresenceModel.id).where(
                PresenceModel.platform == "app", PresenceModel.external_account_id == "yuki-app"
            )
        )
        receipt = await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_presence_id == presence,
                CanonicalEventReceiptModel.event_type == "message:app",
                CanonicalEventReceiptModel.platform_message_id
                == f"{principal.external_account_id}:{request_id}",
            )
        )
        if receipt is None:
            return None
        event = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.canonical_event_id == receipt.canonical_event_id
            )
        )
        if (
            event is None
            or event.author_person_id != principal.person_id
            or event.canonical_conversation_id != principal.conversation_id
            or event.direction != "inbound"
        ):
            raise ClientError("not_found")
        return event

    async def get(self, principal: ClientPrincipal, request_id: str) -> dict[str, Any]:
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_device(session, principal.device_id)
            event = await self.message(session, principal, request_id)
            if event is not None:
                return {
                    "found": True,
                    "operation_kind": "message",
                    "event_id": event.id,
                    "conversation_id": event.canonical_conversation_id,
                }
            command = await personal_command(session, principal.person_id, request_id)
            if command is None:
                return {"found": False}
            return {
                "found": True,
                "operation_kind": command.operation_kind,
                "target": command.operation_ref,
                "status": command.status,
                "result": json.loads(command.effective_state_json)
                if command.effective_state_json
                else None,
            }
