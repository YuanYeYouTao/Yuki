"""Person-authorized views and inputs on the original durable Work."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.json_types import JsonValue
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.work_projection import (
    cursor,
    decode_cursor,
    public_step,
    step_position,
    step_secret,
    steps_token,
    wait_view,
)
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.identity.db_models import IdentityBindingModel, PresenceModel, SpaceBindingModel
from yuki.identity.ingress import IngressPreAdmit
from yuki.identity.person_identity import person_family_condition, same_person_id
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_management import WorkManagementError, manage_work
from yuki.runtime.work_repository import TERMINAL, WorkLease, WorkRepository
from yuki.runtime.work_schema_v1 import effects, inputs, work
from yuki.runtime.work_tree import budget_root_id

if TYPE_CHECKING:
    from yuki.client_api.computer import ClientComputer


def work_access(person_id: str) -> Any:
    return or_(
        person_family_condition(
            func.json_extract(work.c.source_json, "$.actor_person_id"), person_id
        ),
        and_(
            CanonicalConversationModel.kind == "private",
            person_family_condition(CanonicalConversationModel.person_id, person_id),
        ),
    )


def root_work_query(person_id: str, group: str = "active") -> Any:
    return (
        select(work)
        .join(CanonicalConversationModel, CanonicalConversationModel.id == work.c.conversation_id)
        .where(
            work_access(person_id),
            work.c.parent_work_id.is_(None),
            work.c.state.in_(TERMINAL)
            if group == "ended"
            else work.c.state.in_(
                ["queued", "running", "waiting_external", "waiting_user", "suspended"]
            ),
        )
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
        or not event.author_person_id
        or not await same_person_id(session, event.author_person_id, principal.person_id)
        or event.canonical_conversation_id != principal.conversation_id
        or event.origin != "work_input"
        or event.direction != "inbound"
        or event.suppression_status != "keeper"
        or event.platform_message_id != principal.message_key(request_id)
        or mailbox["source_key"] != f"client-input:{principal.message_key(request_id)}"
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
        or not await same_person_id(session, device.person_id, principal.person_id)
    ):
        raise ClientError("not_found")


class ClientWorks:
    def __init__(
        self,
        identity: ClientIdentityService,
        gateway: AppGateway | Mapping[str, AppGateway],
        *,
        computer: ClientComputer | None = None,
    ) -> None:
        self.identity = identity
        self.computer = computer
        self.gateways = dict(gateway) if isinstance(gateway, Mapping) else {"app": gateway}
        self.database = identity.database
        self.repository = WorkRepository(self.database)
        self.step_secret = step_secret(self.database.url)

    async def _public(
        self, session: AsyncSession, row: dict[str, Any], *, detail: bool = True
    ) -> dict[str, Any]:
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
        from yuki.client_api.result_events import confirmed_outbound, result_for_work

        events = list(
            await session.scalars(
                select(ChatEventModel)
                .where(
                    *confirmed_outbound(),
                    result_for_work(row["id"]),
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
        token: str | None
        if detail or row["state"] not in TERMINAL:
            count, token = await steps_token(session, row["id"])
        else:
            count = (
                await session.scalar(
                    select(func.count())
                    .select_from(effects)
                    .where(effects.c.work_id == row["id"], effects.c.kind == "tool")
                )
                or 0
            )
            token = None
        return {
            "work_id": row["id"],
            "conversation_id": row["conversation_id"],
            "generation": row["generation"],
            "state": row["state"],
            "revision": row["revision"],
            "goal": row["goal"] if detail else row["goal"][:240],
            "trigger_event_id": source.get("trigger_event_id"),
            "reason": row["reason"],
            "step_count": count,
            "steps_token": token,
            "help_requested": self.computer.help_for(row["id"]) if self.computer else False,
            "wait": await wait_view(
                session, row["id"], self.computer.run_kind if self.computer else None
            ),
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
            principal = await self.identity.require_principal(session, principal)
            return await self._public(
                session, await personal_work(session, principal.person_id, identity)
            )

    async def steps(
        self, principal: ClientPrincipal, identity: str, after: str | None, limit: int
    ) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            row = await personal_work(session, current.person_id, identity)
            query = select(effects).where(effects.c.work_id == identity, effects.c.kind == "tool")
            if after:
                created, key = await step_position(session, identity, after, self.step_secret)
                query = query.where(
                    tuple_(effects.c.created, effects.c.effect_key) > (created, key)
                )
            rows = (
                (
                    await session.execute(
                        query.order_by(effects.c.created, effects.c.effect_key).limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            selected = rows[:limit]
            children = (
                (
                    await session.execute(
                        select(work.c.id, work.c.goal, work.c.state, work.c.revision)
                        .where(work.c.parent_work_id == identity)
                        .order_by(work.c.created, work.c.id)
                    )
                )
                .mappings()
                .all()
            )
            _, token = await steps_token(session, identity)
            return {
                "steps": [public_step(item, self.step_secret) for item in selected],
                "next_after": cursor(
                    [
                        selected[-1]["created"],
                        public_step(selected[-1], self.step_secret)["step_id"],
                    ]
                )
                if len(rows) > limit
                else None,
                "children": [
                    {
                        "work_id": item["id"],
                        "goal": item["goal"],
                        "state": item["state"],
                        "revision": item["revision"],
                    }
                    for item in children
                ],
                "wait": await wait_view(
                    session, identity, self.computer.run_kind if self.computer else None
                ),
                "steps_token": token,
                "work_revision": row["revision"],
            }

    async def list(
        self, principal: ClientPrincipal, group: str, origin: str, before: str | None, limit: int
    ) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            query = root_work_query(current.person_id, group)
            if origin != "all":
                query = query.where(
                    work.c.conversation_id == current.conversation_id
                    if origin == "this_entry"
                    else work.c.conversation_id != current.conversation_id
                )
            if before:
                value = decode_cursor(before)
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or type(value[0]) not in (float, int)
                    or not math.isfinite(value[0])
                    or not isinstance(value[1], str)
                ):
                    raise ClientError("validation_error")
                query = query.where(tuple_(work.c.updated, work.c.id) < tuple(value))
            rows = (
                (
                    await session.execute(
                        query.order_by(work.c.updated.desc(), work.c.id.desc()).limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            selected = rows[:limit]
            return {
                "works": [
                    await self._public(session, dict(item), detail=False) for item in selected
                ],
                "next_before": cursor([selected[-1]["updated"], selected[-1]["id"]])
                if len(rows) > limit
                else None,
            }

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
            request_person_id=principal.request_person_id,
            request_conversation_id=principal.request_conversation_id,
        )

    async def _replay_input(
        self,
        session: AsyncSession,
        principal: ClientPrincipal,
        identity: str,
        request_id: str,
        bound: str,
    ) -> dict[str, Any] | None:
        previous = await self.identity.replay(
            session, principal.device_id, request_id, bound, principal=principal
        )
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
                    principal = await self.identity.require_principal(reader, principal)
                    row = await personal_work(reader, principal.person_id, identity)
                    actor = json.loads(row["source_json"]).get("actor_person_id")
                    if not actor or not await same_person_id(reader, actor, principal.person_id):
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
                gateway = self.gateways[principal.platform]
                message = gateway.message(
                    binding,
                    request_id,
                    body,
                    (
                        {"type": "text", "data": {"text": body}},
                        {"type": "work_input", "data": {"work_id": identity}},
                    ),
                    conversation_id=principal.request_conversation_id,
                    thread_id=principal.thread_id,
                )
                admitted = await gateway.processor._canonical_ingress.pre_admit(
                    gateway.handle, message
                )
                if admitted.dropped:
                    raise ClientError("unauthenticated")

                async def record_input(
                    writer: AsyncSession,
                    source: IngressPreAdmit = admitted,
                    input_lease: WorkLease | None = lease,
                    request_principal: ClientPrincipal = principal,
                    request_gateway: AppGateway = gateway,
                ) -> dict[str, Any] | ClientError | None:
                    current_principal = await self.identity.require_principal(
                        writer, request_principal
                    )
                    current = await personal_work(writer, current_principal.person_id, identity)
                    actor = json.loads(current["source_json"]).get("actor_person_id")
                    if not actor or not await same_person_id(
                        writer, actor, current_principal.person_id
                    ):
                        raise ClientError("not_found")
                    previous = await self._replay_input(
                        writer, current_principal, identity, request_id, bound
                    )
                    if previous is not None:
                        return previous
                    if current["state"] in TERMINAL:
                        details: dict[str, JsonValue] = {"state": current["state"]}
                        await self._record(
                            writer,
                            current_principal,
                            request_id,
                            operation,
                            identity,
                            bound,
                            details,
                            resource=identity,
                            problem="precondition_failed",
                        )
                        return ClientError("precondition_failed", details=details)
                    else:
                        waiting = current["state"] in {
                            "waiting_user",
                            "waiting_external",
                            "suspended",
                        }
                        if waiting and input_lease is None:
                            return None
                        appended = await request_gateway.processor._canonical_uow.append_inbound(
                            source.message, source, session=writer, origin="work_input"
                        )
                        payload = {
                            "text": body,
                            "origin": "client_input",
                            "source_conversation_id": current_principal.conversation_id,
                            "device_id": current_principal.device_id,
                        }
                        input_id = await self.repository.enqueue(
                            current["conversation_id"],
                            current["generation"],
                            f"client-input:{current_principal.message_key(request_id)}",
                            kind="message",
                            event_id=appended.event.id,
                            work_id=identity,
                            ready=True,
                            payload=payload,
                            session=writer,
                            resume=(input_lease, payload)
                            if waiting and input_lease is not None
                            else None,
                        )
                        result: dict[str, JsonValue] = {
                            "event_id": appended.event.id,
                            "input_id": input_id,
                        }
                        await self._record(
                            writer,
                            current_principal,
                            request_id,
                            operation,
                            identity,
                            bound,
                            result,
                            resource=str(appended.event.id),
                        )
                        return {**result, "replay": False}

                outcome = await self.database.apply_snapshot_write(record_input)
                if isinstance(outcome, ClientError):
                    raise outcome
                if outcome is not None:
                    return outcome
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
            principal = await self.identity.require_principal(writer, principal)
            row = await personal_work(writer, principal.person_id, identity)
            previous = await self.identity.replay(
                writer, principal.device_id, request_id, bound, principal=principal
            )
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
