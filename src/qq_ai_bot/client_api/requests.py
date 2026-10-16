"""Authorized projections of original event and command receipts across devices."""

import json
from typing import Any, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.client_api.db_models import ClientDeviceModel
from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalEventReceiptModel,
    ControlCommandReceiptModel,
)
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.identity.person_identity import person_family_condition, same_person_id
from qq_ai_bot.persistence.models import ChatEventModel


async def personal_command(
    session: AsyncSession, person_id: str, request_id: str
) -> ControlCommandReceiptModel | None:
    return cast(
        ControlCommandReceiptModel | None,
        await session.scalar(
            select(ControlCommandReceiptModel)
            .outerjoin(
                ClientDeviceModel, ClientDeviceModel.id == ControlCommandReceiptModel.principal_id
            )
            .where(
                or_(
                    ControlCommandReceiptModel.request_person_id == person_id,
                    and_(
                        ControlCommandReceiptModel.request_person_id.is_(None),
                        ClientDeviceModel.person_id == person_id,
                    ),
                ),
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
            or not event.author_person_id
            or not await same_person_id(session, event.author_person_id, principal.person_id)
            or event.canonical_conversation_id != principal.conversation_id
            or event.direction != "inbound"
            or event.suppression_status != "keeper"
        ):
            raise ClientError("not_found")
        return event

    async def get(self, principal: ClientPrincipal, request_id: str) -> dict[str, Any]:
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_device(
                session, principal.device_id, binding_id=principal.binding_id
            )
            command = await personal_command(session, principal.request_person_id, request_id)
            if command is not None:
                result = (
                    json.loads(command.effective_state_json)
                    if command.effective_state_json
                    else None
                )
                if command.operation_kind in {"client.work_input", "client.work_cancel"}:
                    from qq_ai_bot.client_api.works import input_result, personal_work

                    assert command.operation_ref is not None
                    await personal_work(session, principal.person_id, command.operation_ref)
                    if command.operation_kind == "client.work_input" and result is not None:
                        await input_result(
                            session,
                            principal,
                            command.operation_ref,
                            result,
                            request_id,
                            command.payload_hash,
                        )
                problem = None
                if command.problem_code:
                    from qq_ai_bot.persistence.models import AdminOperationEventModel

                    audit = await session.get(AdminOperationEventModel, command.audit_id)
                    assert audit is not None
                    problem = {"code": command.problem_code, **json.loads(audit.after_json)}
                return {
                    "found": True,
                    "operation_kind": command.operation_kind,
                    "target": command.operation_ref,
                    "status": command.status,
                    "result": result,
                    "problem_code": command.problem_code,
                    "payload_hash": command.payload_hash,
                    "problem": problem,
                }
            event = await self.message(session, principal, request_id)
            if event is None:
                return {"found": False}
            if event.origin == "work_input":
                raise ClientError("not_found")
            from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
            from qq_ai_bot.social.db_models import SocialOperationModel

            result_ids = list(
                await session.scalars(
                    select(ChatEventModel.id)
                    .join(SocialOperationModel, SocialOperationModel.event_id == ChatEventModel.id)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    )
                    .where(
                        SocialOperationModel.source_conversation_id == principal.conversation_id,
                        SocialOperationModel.source_turn_id.in_(
                            (
                                f"{principal.conversation_id}:event:{event.id}",
                                f"app:{principal.binding_id}:{request_id}",
                            )
                        ),
                        SocialOperationModel.status == "succeeded",
                        ChatEventModel.direction == "outbound",
                        ChatEventModel.suppression_status == "keeper",
                        CanonicalConversationModel.kind == "private",
                        person_family_condition(
                            CanonicalConversationModel.person_id, principal.person_id
                        ),
                    )
                    .order_by(ChatEventModel.id)
                )
            )
            return {
                "found": True,
                "operation_kind": "message",
                "event_id": event.id,
                "conversation_id": event.canonical_conversation_id,
                "result_event_ids": result_ids,
                "target": principal.conversation_id,
                "payload_hash": fingerprint(
                    "message",
                    principal.conversation_id,
                    {
                        "text": json.loads(event.segments_json)[0]["data"]["text"],
                        "attachments": [
                            part["data"]["media_id"]
                            for part in json.loads(event.segments_json)
                            if part.get("data", {}).get("media_id")
                        ],
                    },
                ),
            }
