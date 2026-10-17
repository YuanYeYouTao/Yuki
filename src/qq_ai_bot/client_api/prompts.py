"""Project and cancel an original personal request, without another Agent loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.json_types import JsonValue
from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
from qq_ai_bot.gateway.providers.app.ingress import AppGateway
from qq_ai_bot.identity.person_identity import same_person_id
from qq_ai_bot.persistence.models import AdminOperationEventModel, ChatEventModel
from qq_ai_bot.runtime.work_management import manage_work
from qq_ai_bot.runtime.work_schema_v1 import work

if TYPE_CHECKING:
    from qq_ai_bot.services.processor import ProcessResult

_FAILED_OUTCOMES = frozenset(
    {
        "cancelled",
        "processing_failure",
        "llm_not_configured",
        "empty_llm_response",
        "llm_failure",
        "capacity_failure",
        "validation_failure",
        "send_or_storage_failure",
        "internal_failure",
    }
)
_TERMINAL_WORK = frozenset({"completed", "failed", "cancelled"})


def message_payload(event: ChatEventModel) -> dict[str, Any]:
    segments = json.loads(event.segments_json)
    return {
        "text": segments[0]["data"]["text"],
        "attachments": [
            part["data"]["media_id"] for part in segments if part.get("data", {}).get("media_id")
        ],
    }


async def associated_work(
    session: AsyncSession, principal: ClientPrincipal, event: ChatEventModel
) -> dict[str, Any] | None:
    admission = await session.get(OrdinaryTurnAdmissionModel, event.id)
    if admission is not None and admission.work_id is not None:
        query = select(work).where(
            work.c.id == admission.work_id,
            work.c.conversation_id == event.canonical_conversation_id,
            work.c.generation == admission.generation,
        )
    else:
        query = select(work).where(
            work.c.source_key == f"event:{event.canonical_conversation_id}:{event.id}",
            work.c.conversation_id == event.canonical_conversation_id,
        )
    found = (await session.execute(query)).mappings().first()
    if found is None:
        return None
    row = dict(found)
    source = json.loads(row["source_json"])
    actor = source.get("actor_person_id")
    if not isinstance(actor, str) or not await same_person_id(session, actor, principal.person_id):
        return None
    if admission is None or admission.work_id is None:
        if (
            source.get("trigger_event_id") != event.id
            or source.get("conversation_id") != event.canonical_conversation_id
        ):
            return None
    return row


class ClientPrompts:
    def __init__(self, identity: ClientIdentityService, gateways: Mapping[str, AppGateway]) -> None:
        self.identity = identity
        self.gateways = dict(gateways)
        for gateway in self.gateways.values():
            gateway.on_finished = self.finish

    async def finish(
        self,
        principal: ClientPrincipal,
        request_id: str,
        event_id: int,
        result: ProcessResult | None,
        error: BaseException | None,
    ) -> None:
        from qq_ai_bot.client_api.requests import personal_command

        outcome = (
            "cancelled"
            if isinstance(error, asyncio.CancelledError)
            else "processing_failure"
            if error is not None
            else result.reason
            if result is not None
            else "processing_failure"
        )
        async with self.identity.database.immediate_session() as writer:
            event = await writer.get(ChatEventModel, event_id)
            if (
                event is None
                or event.canonical_conversation_id != principal.conversation_id
                or event.suppression_status != "keeper"
                or not event.author_person_id
                or not await same_person_id(writer, event.author_person_id, principal.person_id)
            ):
                return
            previous = await personal_command(
                writer,
                principal.request_person_id,
                request_id,
                conversation_id=principal.request_conversation_id,
            )
            if previous is not None:
                return
            state: dict[str, JsonValue] = {
                "event_id": event.id,
                "conversation_id": principal.conversation_id,
                "outcome": outcome,
                "handled": result.handled if result is not None else False,
                "sent_messages": result.sent_messages if result is not None else 0,
                "error_category": type(error).__name__ if error is not None else None,
            }
            await self.identity.receipts.record_personal_command(
                writer,
                principal_id=principal.device_id,
                principal_kind="device",
                person_id=principal.person_id,
                request_id=request_id,
                operation="client.message",
                operation_ref=principal.conversation_id,
                payload_hash=fingerprint(
                    "message", principal.conversation_id, message_payload(event)
                ),
                resource_id=str(event.id),
                result=state,
                problem_code=outcome if outcome in _FAILED_OUTCOMES else None,
                request_person_id=principal.request_person_id,
                request_conversation_id=principal.request_conversation_id,
            )

    async def projection(
        self,
        session: AsyncSession,
        principal: ClientPrincipal,
        request_id: str,
        event: ChatEventModel,
        receipt: ControlCommandReceiptModel | None,
    ) -> dict[str, Any]:
        task = self.gateways[principal.platform].request_task(principal.conversation_id, request_id)
        if task is not None and not task.done():
            turn: dict[str, Any] = {"state": "running", "outcome": None}
        elif receipt is not None and receipt.operation_kind == "client.message":
            if receipt.effective_state_json is not None:
                saved = json.loads(receipt.effective_state_json)
            else:
                audit = await session.get(AdminOperationEventModel, receipt.audit_id)
                assert audit is not None
                saved = json.loads(audit.after_json)
            turn = {
                "state": "ended",
                "outcome": saved["outcome"],
            }
            if saved.get("error_category") is not None:
                turn["error_category"] = saved["error_category"]
        else:
            turn = {"state": "unknown", "outcome": None}
        linked = await associated_work(session, principal, event)
        return {
            "turn": turn,
            "work": {
                "work_id": linked["id"],
                "state": linked["state"],
                "revision": linked["revision"],
                "reason": linked["reason"],
            }
            if linked is not None
            else None,
        }

    async def cancel(self, principal: ClientPrincipal, request_id: str) -> dict[str, Any]:
        from qq_ai_bot.client_api.requests import ClientRequests

        requests = ClientRequests(self.identity, prompts=self)
        async with self.identity.database.sessions() as reader:
            principal = await self.identity.require_principal(reader, principal)
            event = await requests.message(reader, principal, request_id)
            if event is None:
                raise ClientError("not_found")
        captured = self.gateways[principal.platform].request_task(
            principal.conversation_id, request_id
        )

        async def stop_original() -> dict[str, Any]:
            cancelled = False
            if captured is not None and not captured.done():
                if not captured.cancelling():
                    captured.cancel()
                await asyncio.gather(captured, return_exceptions=True)
            # Joining the original task closes the race with task_control.accept.
            async with self.identity.database.immediate_session() as writer:
                current = await self.identity.require_principal(writer, principal)
                original = await requests.message(writer, current, request_id)
                if original is None:
                    raise ClientError("not_found")
                linked = await associated_work(writer, current, original)
                if linked is not None:
                    if linked["state"] == "cancelled":
                        cancelled = True
                    elif linked["state"] not in _TERMINAL_WORK:
                        await manage_work(
                            writer,
                            linked["id"],
                            linked["revision"],
                            "cancel",
                            reason="client_prompt_cancelled",
                        )
                        cancelled = True
            result = await requests.get(principal, request_id)
            cancelled = cancelled or result["turn"].get("outcome") == "cancelled"
            return {**result, "cancelled": cancelled}

        operation = asyncio.create_task(stop_original())
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
            operation.result()
            raise
