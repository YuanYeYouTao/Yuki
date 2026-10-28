"""Person-authorized views and inputs on the original durable Work."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, literal_column, or_, select, text, tuple_, union
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.json_types import JsonValue
from yuki.client_api.agent_target import (
    AgentTarget,
    authorize_target,
    input_content,
    stored_target,
    target_segments,
)
from yuki.client_api.agent_target import (
    input_payload as agent_request_payload,
)
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.pagination import cursor, decode_cursor
from yuki.client_api.work_projection import public_step, step_position, steps_token, wait_view
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.identity.canonical_uow import InboundPreparation
from yuki.identity.db_models import IdentityBindingModel, PresenceModel, SpaceBindingModel
from yuki.identity.ingress import IngressPreAdmit
from yuki.identity.person_identity import person_family_condition, same_person_id
from yuki.identity.person_state import ObservationChanged
from yuki.persistence.database import write_failure_committed
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_management import WorkManagementError, manage_work
from yuki.runtime.work_recovery_schema import deliveries
from yuki.runtime.work_repository import TERMINAL, WorkLease, WorkRepository
from yuki.runtime.work_schema_v1 import effects, inputs, work
from yuki.social.db_models import SocialOperationModel

logger = logging.getLogger(__name__)


def _values(values: Iterable[Any]) -> Any:
    requested = func.json_each(json.dumps(tuple(dict.fromkeys(values)))).table_valued("value")
    return select(requested.c.value)


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
            work.c.state.in_(TERMINAL) if group == "ended" else work.c.state.not_in(TERMINAL),
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


async def _input_work(session: AsyncSession, person_id: str, identity: str) -> dict[str, Any]:
    """Select the original input owner without decoding the Work's private source."""
    row = (
        (
            await session.execute(
                select(work.c.id, work.c.conversation_id, work.c.generation, work.c.state)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(
                    work.c.id == identity,
                    person_family_condition(
                        func.json_extract(work.c.source_json, "$.actor_person_id"), person_id
                    ),
                )
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
        or event.origin_key != principal.message_key(request_id)
        or mailbox["source_key"] != f"client-input:{principal.message_key(request_id)}"
        or fingerprint(
            "client.work_input",
            target,
            agent_request_payload(
                json.loads(event.segments_json)[0]["data"]["text"].strip(),
                stored_target(json.loads(event.segments_json)),
            ),
        )
        != bound
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
        computer: Any = None,
    ) -> None:
        self.identity = identity
        self.gateways = dict(gateway) if isinstance(gateway, Mapping) else {"app": gateway}
        self.database = identity.database
        self.repository = WorkRepository(self.database)
        self.computer = computer

    async def linked_for_events(
        self,
        session: AsyncSession,
        person_id: str,
        event_ids: Iterable[int],
    ) -> dict[int, list[str]]:
        ids = tuple(event_ids)
        if not ids:
            return {}
        trigger = func.json_extract(work.c.source_json, literal_column("'$.trigger_event_id'"))
        domain = func.json_extract(
            effects.c.receipt_json, literal_column("'$.invocation.original_domain_ref'")
        )
        delivered = (
            select(SocialOperationModel.id, SocialOperationModel.event_id)
            .join(ChatEventModel, ChatEventModel.id == SocialOperationModel.event_id)
            .where(
                ChatEventModel.id.in_(_values(ids)),
                ChatEventModel.direction == "outbound",
                ChatEventModel.author_kind == "yuki",
                ChatEventModel.suppression_status == "keeper",
                SocialOperationModel.status == "succeeded",
            )
            .subquery("page_delivered_operations")
        )
        links = union(
            select(trigger.label("event_id"), work.c.id.label("work_id")).where(
                trigger.in_(_values(ids))
            ),
            select(delivered.c.event_id, deliveries.c.work_id).join(
                deliveries, deliveries.c.id == delivered.c.id
            ),
            select(delivered.c.event_id, effects.c.work_id).join(
                effects, domain == "social:" + delivered.c.id
            ),
        ).subquery("page_work_links")
        rows = await session.execute(
            select(links.c.event_id, work.c.id)
            .join(work, work.c.id == links.c.work_id)
            .join(
                CanonicalConversationModel, CanonicalConversationModel.id == work.c.conversation_id
            )
            .where(work_access(person_id))
            .order_by(links.c.event_id, work.c.created, work.c.id)
        )
        result: dict[int, list[str]] = {identity: [] for identity in ids}
        for event_id, work_id in rows:
            result[event_id].append(work_id)
        return result

    async def public_many(
        self, session: AsyncSession, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = tuple(row["id"] for row in rows)
        sources = {row["id"]: json.loads(row["source_json"]) for row in rows}
        conversations = {
            row.id: row
            for row in await session.scalars(
                select(CanonicalConversationModel).where(
                    CanonicalConversationModel.id.in_(
                        _values(row["conversation_id"] for row in rows)
                    )
                )
            )
        }
        presences = {
            row.id: row
            for row in await session.scalars(
                select(PresenceModel).where(
                    PresenceModel.id.in_(
                        _values(
                            source["presence_id"]
                            for source in sources.values()
                            if source.get("presence_id")
                        )
                    )
                )
            )
        }
        bindings = {
            row.id: row
            for row in await session.scalars(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.id.in_(
                        _values(
                            row.identity_binding_id
                            for row in conversations.values()
                            if row.identity_binding_id
                        )
                    )
                )
            )
        }
        space_bindings: dict[str | None, SpaceBindingModel] = {
            row.id: row
            for row in await session.scalars(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.id.in_(
                        _values(
                            row.space_binding_id
                            for row in conversations.values()
                            if row.space_binding_id
                        )
                    )
                )
            )
        }
        lineage = (
            select(work.c.id.label("origin"), work.c.id, work.c.parent_work_id)
            .where(work.c.id.in_(_values(ids)))
            .cte("client_work_ancestors", recursive=True)
        )
        parent = work.alias("client_parent")
        lineage = lineage.union_all(
            select(lineage.c.origin, parent.c.id, parent.c.parent_work_id).join(
                parent, parent.c.id == lineage.c.parent_work_id
            )
        )
        roots = {
            str(row[0]): str(row[1])
            for row in (
                await session.execute(
                    select(lineage.c.origin, lineage.c.id).where(lineage.c.parent_work_id.is_(None))
                )
            ).all()
        }
        root_budgets = {
            row["root_id"]: row
            for row in (
                await session.execute(
                    select(budgets).where(budgets.c.root_id.in_(_values(roots.values())))
                )
            ).mappings()
        }
        children = {
            str(row[0]): int(row[1])
            for row in (
                await session.execute(
                    select(work.c.parent_work_id, func.count())
                    .where(work.c.parent_work_id.in_(_values(ids)))
                    .group_by(work.c.parent_work_id)
                )
            ).all()
        }
        domain = func.json_extract(
            effects.c.receipt_json, literal_column("'$.invocation.original_domain_ref'")
        )
        links = union(
            select(deliveries.c.work_id, deliveries.c.id.label("operation_id")).where(
                deliveries.c.work_id.in_(_values(ids))
            ),
            select(effects.c.work_id, func.substr(domain, 8).label("operation_id")).where(
                effects.c.work_id.in_(_values(ids)), domain.like("social:%")
            ),
        ).subquery("client_work_social")
        result_rows = (
            await session.execute(
                select(links.c.work_id, ChatEventModel)
                .join(SocialOperationModel, SocialOperationModel.id == links.c.operation_id)
                .join(ChatEventModel, ChatEventModel.id == SocialOperationModel.event_id)
                .where(
                    SocialOperationModel.status == "succeeded",
                    ChatEventModel.direction == "outbound",
                    ChatEventModel.author_kind == "yuki",
                    ChatEventModel.suppression_status == "keeper",
                )
                .order_by(ChatEventModel.id)
            )
        ).all()
        results: dict[str, dict[int, dict[str, Any]]] = {}
        for work_id, event in result_rows:
            results.setdefault(work_id, {})[event.id] = {
                "event_id": event.id,
                "conversation_id": event.canonical_conversation_id,
                "text": event.content,
                "artifacts": [
                    part["data"]
                    for part in json.loads(event.segments_json)
                    if isinstance(part.get("data"), dict) and part["data"].get("artifact_id")
                ],
            }
        step_counts = {
            row[0]: (row[1], f"{row[1]}:{row[2]}")
            for row in await session.execute(
                select(effects.c.work_id, func.count(), func.max(effects.c.updated))
                .where(effects.c.work_id.in_(_values(ids)), effects.c.kind == "tool")
                .group_by(effects.c.work_id)
            )
        }
        projections = []
        for row in rows:
            conversation = conversations[row["conversation_id"]]
            source = sources[row["id"]]
            presence = presences.get(source.get("presence_id"))
            binding: IdentityBindingModel | SpaceBindingModel | None = (
                bindings.get(conversation.identity_binding_id)
                if conversation.identity_binding_id
                else None
            )
            if conversation.kind == "space":
                binding = space_bindings.get(conversation.space_binding_id)
            origin = {
                "conversation_id": conversation.id,
                "kind": conversation.kind,
                "binding_id": binding.id if binding else None,
                "platform": presence.platform
                if presence
                else binding.platform
                if binding
                else None,
                "display_name": binding.display_name if binding else "",
                "space_id": conversation.space_id,
            }
            root = roots[row["id"]]
            budget = root_budgets.get(root)
            delivered = list(results.get(row["id"], {}).values())
            projection = {
                "work_id": row["id"],
                "conversation_id": row["conversation_id"],
                "generation": row["generation"],
                "state": row["state"],
                "revision": row["revision"],
                "goal": row["goal"][:240],
                "trigger_event_id": source.get("trigger_event_id"),
                "reason": row["reason"],
                "step_count": step_counts.get(row["id"], (0, "0:0"))[0],
                "steps_token": step_counts.get(row["id"], (0, "0:0"))[1],
                "source": origin,
                "result_delivery": origin,
                "waiting_kind": "user"
                if row["state"] == "waiting_user"
                else "external"
                if row["state"] == "waiting_external"
                else None,
                "waiting_question": delivered[-1]
                if row["state"] == "waiting_user" and delivered
                else None,
                "child_count": children.get(row["id"], 0),
                "budget": {
                    "root_id": root,
                    "models": budget["models"] if budget else row["model_requests"],
                    "model_limit": budget["model_limit"] if budget else None,
                    "tools": budget["tools"] if budget else row["tool_calls"],
                    "tool_limit": budget["tool_limit"] if budget else None,
                },
                "results": delivered,
                "updated_at": row["updated"],
            }
            if self.computer is not None:
                projection["help_requested"] = self.computer.help_for(row["id"])
            projections.append(projection)
        return projections

    async def get(self, principal: ClientPrincipal, identity: str) -> dict[str, Any]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            row = await personal_work(session, principal.person_id, identity)
            result = (await self.public_many(session, [row]))[0]
            result.update(
                goal=row["goal"],
                wait=await wait_view(
                    session, identity, self.computer.run_kind if self.computer else None
                ),
            )
            return result

    async def list(
        self, principal: ClientPrincipal, group: str, origin: str, before: str | None, limit: int
    ) -> dict[str, Any]:
        if (
            group not in {"active", "ended"}
            or origin not in {"all", "this_entry", "other"}
            or limit < 1
        ):
            raise ClientError("validation_error")
        async with self.database.read_session() as session:
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
            rows = list(
                (
                    await session.execute(
                        query.order_by(work.c.updated.desc(), work.c.id.desc()).limit(limit + 1)
                    )
                ).mappings()
            )
            selected = rows[:limit]
            return {
                "works": await self.public_many(session, [dict(row) for row in selected]),
                "next_before": cursor([selected[-1]["updated"], selected[-1]["id"]])
                if len(rows) > limit
                else None,
            }

    async def steps(
        self, principal: ClientPrincipal, identity: str, after: str | None, limit: int
    ) -> dict[str, Any]:
        if limit < 1:
            raise ClientError("validation_error")
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            row = await personal_work(session, current.person_id, identity)
            # An opaque presentation ID is not a capability. Resource ACL remains above.
            namespace = identity.encode()
            query = select(effects).where(effects.c.work_id == identity, effects.c.kind == "tool")
            if after:
                created, key = await step_position(session, identity, after, namespace)
                query = query.where(
                    tuple_(effects.c.created, effects.c.effect_key) > (created, key)
                )
            rows = list(
                (
                    await session.execute(
                        query.order_by(effects.c.created, effects.c.effect_key).limit(limit + 1)
                    )
                ).mappings()
            )
            selected = rows[:limit]
            children = (
                await session.execute(
                    select(work.c.id, work.c.goal, work.c.state, work.c.revision)
                    .where(work.c.parent_work_id == identity)
                    .order_by(work.c.created, work.c.id)
                )
            ).mappings()
            _, token = await steps_token(session, identity)
            return {
                "steps": [public_step(item, namespace) for item in selected],
                "next_after": cursor(
                    [selected[-1]["created"], public_step(selected[-1], namespace)["step_id"]]
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
        self,
        principal: ClientPrincipal,
        identity: str,
        request_id: str,
        body: str,
        agent_target: AgentTarget | None = None,
    ) -> dict[str, Any]:
        body = body.strip()
        if not body and agent_target is None:
            raise ClientError("validation_error")
        operation = "client.work_input"
        bound = fingerprint(operation, identity, agent_request_payload(body, agent_target))
        original_body = body
        body = input_content(body, agent_target)
        canonical_event_id = str(uuid4())
        while True:
            lease = None
            outcome: dict[str, Any] | ClientError | None = None
            try:
                async with self.database.read_session() as reader:
                    principal = await self.identity.require_principal(reader, principal)
                    row = await _input_work(reader, principal.person_id, identity)
                    previous = await self._replay_input(
                        reader, principal, identity, request_id, bound
                    )
                    if previous is not None:
                        return previous
                await authorize_target(self.database, principal.person_id, agent_target)
                waiting = row["state"] in {"waiting_user", "waiting_external", "suspended"}
                if waiting:
                    lease = await self.repository.acquire(row["conversation_id"], row["generation"])
                    if lease is None:
                        raise ClientError("unavailable")
                binding = None
                async with self.database.read_session() as reader:
                    binding = await reader.get(IdentityBindingModel, principal.binding_id)
                assert binding is not None
                gateway = self.gateways[principal.platform]
                message = gateway.message(
                    binding,
                    request_id,
                    body,
                    (
                        {"type": "text", "data": {"text": original_body}},
                        {"type": "work_input", "data": {"work_id": identity}},
                        *target_segments(agent_target),
                    ),
                    conversation_id=principal.conversation_id,
                    thread_id=principal.thread_id,
                )
                admitted = await gateway.processor._canonical_ingress.pre_admit(
                    gateway.handle, message
                )
                if admitted.dropped:
                    raise ClientError("unauthenticated")

                from yuki.persistence.history_search import prepare_terms

                prepared_terms = await asyncio.to_thread(prepare_terms, admitted.message.text)

                from yuki.runtime.work_repository import encode_json

                input_payload = {
                    "text": body,
                    "origin": "client_input",
                    "source_conversation_id": principal.conversation_id,
                    "device_id": principal.device_id,
                }
                encoded_payload = encode_json(input_payload)
                prepared_inbound = await gateway.processor._canonical_uow.prepare_inbound(
                    admitted.message,
                    admitted,
                    canonical_event_id=canonical_event_id,
                    origin="work_input",
                    prepared_terms=prepared_terms,
                )

                async def record_input(
                    writer: AsyncSession,
                    source: IngressPreAdmit = admitted,
                    input_terms: tuple[str, ...] = prepared_terms,
                    input_lease: WorkLease | None = lease,
                    request_principal: ClientPrincipal = principal,
                    request_gateway: AppGateway = gateway,
                    inbound_preparation: InboundPreparation = prepared_inbound,
                    input_payload_json: str = encoded_payload,
                ) -> dict[str, Any] | ClientError | None:
                    current_principal = await self.identity.require_principal(
                        writer, request_principal
                    )
                    current = await _input_work(writer, current_principal.person_id, identity)
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
                            source.message,
                            source,
                            session=writer,
                            origin="work_input",
                            prepared_terms=input_terms,
                            prepared=inbound_preparation,
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
                            prepared_payload=input_payload_json,
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

                async with self.database.immediate_session() as writer:
                    outcome = await record_input(writer)
            except BaseException as original_error:
                if lease is not None:
                    try:
                        await self.repository.release(lease)
                    except asyncio.CancelledError:
                        raise
                    except Exception as cleanup_error:
                        original_error.add_note(
                            f"client input lease release failed: {type(cleanup_error).__name__}"
                        )
                if (
                    isinstance(original_error, ObservationChanged)
                    and write_failure_committed(original_error) is False
                ):
                    continue
                raise
            else:
                if lease is not None:
                    try:
                        await self.repository.release(lease)
                    except asyncio.CancelledError:
                        raise
                    except Exception as cleanup_error:
                        if outcome is None:
                            raise
                        # The original input, event and UUID receipt are committed.
                        # This short borrowed lease can expire without replaying them.
                        logger.warning(
                            "client_work_input_lease_release_failed category=%s",
                            type(cleanup_error).__name__,
                        )
                if isinstance(outcome, ClientError):
                    raise outcome
                if outcome is not None:
                    return outcome

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
