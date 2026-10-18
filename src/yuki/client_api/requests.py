"""Authorized projections of original event and command receipts across devices."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.conversation.canonical_db_models import (
    CanonicalEventReceiptModel,
    ControlCommandReceiptModel,
)
from yuki.identity.db_models import PresenceModel
from yuki.identity.person_identity import person_family_condition, same_person_id
from yuki.persistence.models import ChatEventModel

if TYPE_CHECKING:
    from yuki.client_api.prompts import ClientPrompts


async def personal_command(
    session: AsyncSession,
    person_id: str,
    request_id: str,
    *,
    conversation_id: str | None = None,
) -> ControlCommandReceiptModel | None:
    scope = (
        ControlCommandReceiptModel.request_conversation_id == conversation_id
        if conversation_id is not None
        else and_(
            ControlCommandReceiptModel.request_conversation_id.is_(None),
            or_(
                ControlCommandReceiptModel.request_person_id == person_id,
                and_(
                    ControlCommandReceiptModel.request_person_id.is_(None),
                    ClientDeviceModel.person_id == person_id,
                ),
            ),
        )
    )
    return cast(
        ControlCommandReceiptModel | None,
        await session.scalar(
            select(ControlCommandReceiptModel)
            .outerjoin(
                ClientDeviceModel, ClientDeviceModel.id == ControlCommandReceiptModel.principal_id
            )
            .where(
                scope,
                ControlCommandReceiptModel.request_id == request_id,
            )
        ),
    )


class ClientRequests:
    def __init__(
        self, identity: ClientIdentityService, *, prompts: ClientPrompts | None = None
    ) -> None:
        self.identity = identity
        self.prompts = prompts

    async def message(
        self, session: AsyncSession, principal: ClientPrincipal, request_id: str
    ) -> ChatEventModel | None:
        presence = await session.scalar(
            select(PresenceModel.id).where(
                PresenceModel.platform == principal.platform,
                PresenceModel.external_account_id == f"yuki-{principal.platform}",
            )
        )
        receipt = await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_presence_id == presence,
                CanonicalEventReceiptModel.event_type == f"message:{principal.platform}",
                CanonicalEventReceiptModel.platform_message_id == principal.message_key(request_id),
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
        # Observe the live owner before reading receipts: it may commit its
        # final receipt and exit while this query still holds an earlier view.
        running = (
            self.prompts.is_running(principal, request_id) if self.prompts is not None else False
        )
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_principal(session, principal)
            command = await personal_command(
                session,
                principal.request_person_id,
                request_id,
                conversation_id=principal.request_conversation_id,
            )
            if command is not None and command.operation_kind != "client.message":
                result = (
                    json.loads(command.effective_state_json)
                    if command.effective_state_json
                    else None
                )
                if command.operation_kind in {"client.work_input", "client.work_cancel"}:
                    from yuki.client_api.works import input_result, personal_work

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
                    from yuki.persistence.models import AdminOperationEventModel

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
            from yuki.conversation.canonical_db_models import CanonicalConversationModel
            from yuki.social.db_models import SocialOperationModel

            status = (
                await self.prompts.projection(session, principal, event, command, running=running)
                if self.prompts is not None
                else {"turn": {"state": "unknown", "outcome": None}, "work": None}
            )
            sources: list[ColumnElement[bool]] = [
                SocialOperationModel.source_turn_id.in_(
                    (
                        f"{principal.conversation_id}:event:{event.id}",
                        f"{principal.platform}:{principal.binding_id}:"
                        + (f"{principal.conversation_id}:" if principal.platform == "acp" else "")
                        + request_id,
                    )
                )
            ]
            if status["work"] is not None:
                from yuki.runtime.work_recovery_schema import deliveries
                from yuki.runtime.work_schema_v1 import effects

                work_id = status["work"]["work_id"]
                sources.extend(
                    (
                        select(deliveries.c.id)
                        .where(
                            deliveries.c.work_id == work_id,
                            deliveries.c.id == SocialOperationModel.id,
                        )
                        .exists(),
                        select(effects.c.effect_key)
                        .where(
                            effects.c.work_id == work_id,
                            func.json_extract(
                                effects.c.receipt_json, "$.invocation.original_domain_ref"
                            )
                            == "social:" + SocialOperationModel.id,
                        )
                        .exists(),
                    )
                )
            result_query = (
                select(ChatEventModel.id)
                .join(SocialOperationModel, SocialOperationModel.event_id == ChatEventModel.id)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                )
                .where(
                    SocialOperationModel.source_conversation_id == principal.conversation_id,
                    or_(*sources),
                    SocialOperationModel.status == "succeeded",
                    ChatEventModel.direction == "outbound",
                    ChatEventModel.suppression_status == "keeper",
                    CanonicalConversationModel.kind == "private",
                    person_family_condition(
                        CanonicalConversationModel.person_id, principal.person_id
                    ),
                    *(
                        (ChatEventModel.canonical_conversation_id == principal.conversation_id,)
                        if principal.platform == "acp"
                        else ()
                    ),
                )
                .order_by(ChatEventModel.id)
            )
            from yuki.client_api.prompts import message_payload

            problem = None
            if command is not None and command.problem_code:
                from yuki.persistence.models import AdminOperationEventModel

                audit = await session.get(AdminOperationEventModel, command.audit_id)
                assert audit is not None
                problem = {"code": command.problem_code, **json.loads(audit.after_json)}
            facts = {
                "found": True,
                "operation_kind": "message",
                "event_id": event.id,
                "conversation_id": event.canonical_conversation_id,
                "target": principal.conversation_id,
                "problem_code": command.problem_code if command is not None else None,
                "problem": problem,
                "payload_hash": fingerprint(
                    "message",
                    principal.conversation_id,
                    message_payload(event),
                ),
                **status,
            }
        # A live activation can finish after the first SQLite snapshot began.
        # Read its confirmed sends in a fresh snapshot after observing its exit.
        async with self.identity.database.sessions() as reader:
            result_ids = list(await reader.scalars(result_query))
        return {**facts, "result_event_ids": result_ids}
