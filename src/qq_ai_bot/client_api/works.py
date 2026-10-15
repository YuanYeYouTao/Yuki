"""Person-authorized views and inputs on the original durable Work."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.json_types import JsonValue
from qq_ai_bot.client_api.db_models import ClientDeviceModel
from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from qq_ai_bot.gateway.providers.app.ingress import AppGateway
from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel, SpaceBindingModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_management import WorkManagementError, manage_work
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import TERMINAL, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, work
from qq_ai_bot.runtime.work_tree import budget_root_id
from qq_ai_bot.social.db_models import SocialOperationModel


def work_access(person_id: str) -> Any:
    return or_(
        func.json_extract(work.c.source_json, "$.actor_person_id") == person_id,
        and_(
            CanonicalConversationModel.kind == "private",
            CanonicalConversationModel.person_id == person_id,
        ),
    )


async def personal_work(session: AsyncSession, person_id: str, identity: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                select(work)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(work.c.id == identity, work_access(person_id))
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ClientError("not_found")
    return dict(row)


async def input_result(
    session: AsyncSession,
    principal: ClientPrincipal,
    target: str,
    result: dict[str, Any],
    request_id: str,
    bound: str,
) -> None:
    event = await session.get(ChatEventModel, result["event_id"])
    mailbox = (
        (
            await session.execute(
                select(inputs).where(
                    inputs.c.id == result["input_id"],
                    inputs.c.work_id == target,
                    inputs.c.event_id == result["event_id"],
                )
            )
        )
        .mappings()
        .first()
    )
    if (
        event is None
        or mailbox is None
        or event.author_person_id != principal.person_id
        or event.canonical_conversation_id != principal.conversation_id
        or event.origin != "work_input"
        or event.direction != "inbound"
        or event.suppression_status != "keeper"
        or event.platform_message_id != f"{principal.external_account_id}:{request_id}"
        or mailbox["source_key"] != f"client-input:{principal.external_account_id}:{request_id}"
        or fingerprint("client.work_input", target, {"text": event.content.strip()}) != bound
    ):
        raise ClientError("not_found")
    payload = json.loads(mailbox["payload_json"])
    device = (
        await session.get(ClientDeviceModel, payload.get("device_id"))
        if payload.get("device_id")
        else None
    )
    if (
        payload.get("text") != event.content
        or payload.get("origin") != "client_input"
        or payload.get("source_conversation_id") != event.canonical_conversation_id
        or device is None
        or device.person_id != principal.person_id
    ):
        raise ClientError("not_found")


class ClientWorks:
    def __init__(self, identity: ClientIdentityService, gateway: AppGateway) -> None:
        self.identity, self.gateway = identity, gateway
        self.database = identity.database
        self.repository = WorkRepository(self.database)

    async def _public(self, session: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
        conversation = await session.get(CanonicalConversationModel, row["conversation_id"])
        assert conversation is not None
        source = json.loads(row["source_json"])
        presence = (
            await session.get(PresenceModel, source.get("presence_id"))
            if source.get("presence_id")
            else None
        )
        binding: IdentityBindingModel | SpaceBindingModel | None = (
            await session.get(IdentityBindingModel, conversation.identity_binding_id)
            if conversation.identity_binding_id
            else None
        )
        if conversation.kind == "space":
            trigger = (
                await session.get(ChatEventModel, source.get("trigger_event_id"))
                if source.get("trigger_event_id")
                else None
            )
            binding = None
            if (
                presence is not None
                and trigger is not None
                and trigger.canonical_conversation_id == conversation.id
            ):
                binding = await session.scalar(
                    select(SpaceBindingModel).where(
                        SpaceBindingModel.space_id == conversation.space_id,
                        SpaceBindingModel.platform == presence.platform,
                        SpaceBindingModel.external_space_id == trigger.group_id,
                    )
                )
        origin = {
            "conversation_id": conversation.id,
            "kind": conversation.kind,
            "binding_id": binding.id if binding else None,
            "platform": presence.platform if presence else (binding.platform if binding else None),
            "display_name": binding.display_name if binding else "",
            "space_id": conversation.space_id,
        }
        root = await budget_root_id(session, row["id"])
        budget = (
            (await session.execute(select(budgets).where(budgets.c.root_id == root)))
            .mappings()
            .first()
        )
        events = list(
            await session.scalars(
                select(ChatEventModel)
                .join(SocialOperationModel, SocialOperationModel.event_id == ChatEventModel.id)
                .where(
                    SocialOperationModel.status == "succeeded",
                    ChatEventModel.direction == "outbound",
                    ChatEventModel.author_kind == "yuki",
                    or_(
                        select(deliveries.c.id)
                        .where(
                            deliveries.c.work_id == row["id"],
                            deliveries.c.id == SocialOperationModel.id,
                        )
                        .exists(),
                        select(effects.c.effect_key)
                        .where(
                            effects.c.work_id == row["id"],
                            func.json_extract(
                                effects.c.receipt_json, "$.invocation.original_domain_ref"
                            )
                            == "social:" + SocialOperationModel.id,
                        )
                        .exists(),
                    ),
                )
                .order_by(ChatEventModel.id)
            )
        )
        results = []
        for event in events:
            artifacts = [
                segment["data"]
                for segment in json.loads(event.segments_json)
                if isinstance(segment.get("data"), dict) and segment["data"].get("artifact_id")
            ]
            results.append(
                {
                    "event_id": event.id,
                    "conversation_id": event.canonical_conversation_id,
                    "text": event.content,
                    "artifacts": artifacts,
                }
            )
        return {
            "work_id": row["id"],
            "conversation_id": row["conversation_id"],
            "generation": row["generation"],
            "state": row["state"],
            "revision": row["revision"],
            "goal": row["goal"][:240],
            "source": origin,
            "result_delivery": origin,
            "waiting_kind": "user"
            if row["state"] == "waiting_user"
            else "external"
            if row["state"] == "waiting_external"
            else None,
            "waiting_question": results[-1] if row["state"] == "waiting_user" and results else None,
            "child_count": await session.scalar(
                select(func.count()).select_from(work).where(work.c.parent_work_id == row["id"])
            ),
            "budget": {
                "root_id": root,
                "models": budget["models"] if budget else row["model_requests"],
                "model_limit": budget["model_limit"] if budget else None,
                "tools": budget["tools"] if budget else row["tool_calls"],
                "tool_limit": budget["tool_limit"] if budget else None,
            },
            "results": results,
            "updated_at": row["updated"],
        }

    async def get(self, principal: ClientPrincipal, identity: str) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_device(session, principal.device_id)
            return await self._public(
                session, await personal_work(session, principal.person_id, identity)
            )

    async def home(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_device(session, principal.device_id)
            rows = (
                (
                    await session.execute(
                        select(work)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id == work.c.conversation_id,
                        )
                        .where(work_access(principal.person_id))
                        .order_by(work.c.updated.desc(), work.c.id)
                    )
                )
                .mappings()
                .all()
            )
            return {"works": [await self._public(session, dict(row)) for row in rows]}

    async def _record(
        self,
        session: AsyncSession,
        principal: ClientPrincipal,
        request_id: str,
        operation: str,
        target: str,
        bound: str,
        result: dict[str, JsonValue],
        *,
        resource: str,
        problem: str | None = None,
    ) -> None:
        await self.identity.receipts.record_personal_command(
            session,
            principal_id=principal.device_id,
            principal_kind="device",
            person_id=principal.person_id,
            request_id=request_id,
            operation=operation,
            operation_ref=target,
            payload_hash=bound,
            resource_id=resource,
            result=result,
            problem_code=problem,
        )

    async def _replay_input(
        self,
        session: AsyncSession,
        principal: ClientPrincipal,
        identity: str,
        request_id: str,
        bound: str,
    ) -> dict[str, Any] | None:
        previous = await self.identity.replay(session, principal.device_id, request_id, bound)
        if previous is not None:
            await input_result(session, principal, identity, previous, request_id, bound)
        return previous

    async def submit(
        self, principal: ClientPrincipal, identity: str, request_id: str, body: str
    ) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise ClientError("validation_error")
        operation = "client.work_input"
        bound = fingerprint(operation, identity, {"text": body})
        while True:
            lease = None
            try:
                async with self.database.sessions() as reader:
                    principal = await self.identity.require_device(reader, principal.device_id)
                    row = await personal_work(reader, principal.person_id, identity)
                    if json.loads(row["source_json"]).get("actor_person_id") != principal.person_id:
                        raise ClientError("not_found")
                    previous = await self._replay_input(
                        reader, principal, identity, request_id, bound
                    )
                    if previous is not None:
                        return previous
                waiting = row["state"] in {"waiting_user", "waiting_external", "suspended"}
                if waiting:
                    lease = await self.repository.acquire(row["conversation_id"], row["generation"])
                    if lease is None:
                        raise ClientError("unavailable")
                binding = None
                async with self.database.sessions() as reader:
                    binding = await reader.get(IdentityBindingModel, principal.binding_id)
                assert binding is not None
                message = self.gateway.message(
                    binding,
                    request_id,
                    body,
                    (
                        {"type": "text", "data": {"text": body}},
                        {"type": "work_input", "data": {"work_id": identity}},
                    ),
                )
                admitted = await self.gateway.processor._canonical_ingress.pre_admit(
                    self.gateway.handle, message
                )
                if admitted.dropped:
                    raise ClientError("unauthenticated")
                failure = None
                async with self.database.immediate_session() as writer:
                    principal = await self.identity.require_device(writer, principal.device_id)
                    current = await personal_work(writer, principal.person_id, identity)
                    if (
                        json.loads(current["source_json"]).get("actor_person_id")
                        != principal.person_id
                    ):
                        raise ClientError("not_found")
                    previous = await self._replay_input(
                        writer, principal, identity, request_id, bound
                    )
                    if previous is not None:
                        return previous
                    if current["state"] in TERMINAL:
                        details: dict[str, JsonValue] = {"state": current["state"]}
                        await self._record(
                            writer,
                            principal,
                            request_id,
                            operation,
                            identity,
                            bound,
                            details,
                            resource=identity,
                            problem="precondition_failed",
                        )
                        failure = ClientError("precondition_failed", details=details)
                    else:
                        waiting = current["state"] in {
                            "waiting_user",
                            "waiting_external",
                            "suspended",
                        }
                        if waiting and lease is None:
                            continue
                        appended = await self.gateway.processor._canonical_uow.append_inbound(
                            admitted.message, admitted, session=writer, origin="work_input"
                        )
                        payload = {
                            "text": body,
                            "origin": "client_input",
                            "source_conversation_id": principal.conversation_id,
                            "device_id": principal.device_id,
                        }
                        input_id = await self.repository.enqueue(
                            current["conversation_id"],
                            current["generation"],
                            f"client-input:{principal.external_account_id}:{request_id}",
                            kind="message",
                            event_id=appended.event.id,
                            work_id=identity,
                            ready=True,
                            payload=payload,
                            session=writer,
                            resume=(lease, payload) if waiting and lease is not None else None,
                        )
                        result: dict[str, JsonValue] = {
                            "event_id": appended.event.id,
                            "input_id": input_id,
                        }
                        await self._record(
                            writer,
                            principal,
                            request_id,
                            operation,
                            identity,
                            bound,
                            result,
                            resource=str(appended.event.id),
                        )
                if failure is not None:
                    raise failure
                return {**result, "replay": False}
            finally:
                if lease is not None:
                    await self.repository.release(lease)

    async def cancel(
        self, principal: ClientPrincipal, identity: str, request_id: str, revision: int
    ) -> dict[str, Any]:
        operation = "client.work_cancel"
        bound = fingerprint(operation, identity, {"expected_revision": revision})
        failure = None
        async with self.database.immediate_session() as writer:
            principal = await self.identity.require_device(writer, principal.device_id)
            row = await personal_work(writer, principal.person_id, identity)
            previous = await self.identity.replay(writer, principal.device_id, request_id, bound)
            if previous is not None:
                return previous
            try:
                new_revision, state = await manage_work(
                    writer, identity, revision, "cancel", reason="person_cancelled"
                )
                result: dict[str, JsonValue] = {
                    "work_id": identity,
                    "state": state,
                    "revision": new_revision,
                }
                await self._record(
                    writer,
                    principal,
                    request_id,
                    operation,
                    identity,
                    bound,
                    result,
                    resource=identity,
                )
            except WorkManagementError as exc:
                details: dict[str, JsonValue] = {"state": row["state"], "revision": row["revision"]}
                await self._record(
                    writer,
                    principal,
                    request_id,
                    operation,
                    identity,
                    bound,
                    details,
                    resource=identity,
                    problem=exc.code,
                )
                failure = ClientError(exc.code, details=details)
        if failure is not None:
            raise failure
        return {**result, "replay": False}
