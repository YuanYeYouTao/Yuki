"""Personal facts retain their subject, evidence ACL and original command receipts."""

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.pagination import cursor, decode_cursor
from yuki.client_api.requests import ClientRequests, personal_command
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition, person_family_ids
from yuki.memory.authorized_scope import AuthorizedMemoryScope, authorized_fact_condition
from yuki.memory.dream.repository import fact_signature
from yuki.memory.enums import MemoryScopeType
from yuki.memory.models import MemoryFact
from yuki.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
)
from yuki.memory.mutation.service import MemoryMutationRejected, MemoryMutationService
from yuki.memory.partition import MemoryPartitionResolutionError
from yuki.memory.projections import project_memory_fact_rows
from yuki.memory.repository import readable_evidence_predicate
from yuki.memory.service import MemoryFactService
from yuki.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord


def fact_view(fact: MemoryFact) -> dict[str, Any]:
    return {
        "fact_id": fact.id,
        "kind": fact.kind.value,
        "key": fact.memory_key,
        "text": fact.content,
        "confidence": fact.confidence,
        "revision": fact_signature(fact),
        "updated_at": fact.updated_at.isoformat(),
        "evidence_count": fact.evidence_count,
        "scope": fact.scope_type.value,
        "writable": fact.scope_type is MemoryScopeType.PERSON,
    }


class ClientMemory:
    def __init__(
        self,
        identity: ClientIdentityService,
        facts: MemoryFactService,
        mutations: MemoryMutationService,
        gateway: AppGateway,
    ) -> None:
        self.identity, self.facts, self.mutations, self.gateway = (
            identity,
            facts,
            mutations,
            gateway,
        )

    async def _conditions(
        self, session: AsyncSession, principal: ClientPrincipal, about: str | None
    ) -> list[Any]:
        scope = AuthorizedMemoryScope(
            requester_person_id=principal.person_id,
            requester_family_ids=await person_family_ids(session, principal.person_id),
            current_private_person_id=principal.person_id,
            allowed_scopes=(MemoryScopeType.PERSON, MemoryScopeType.SELF),
        )
        own_person = (MemoryFactModel.scope_type == "person") & person_family_condition(
            MemoryFactModel.canonical_subject_person_id, principal.person_id
        )
        selected = (
            own_person
            if about == "me"
            else MemoryFactModel.scope_type == "self"
            if about == "self"
            else or_(own_person, MemoryFactModel.scope_type == "self")
        )
        return [
            selected,
            authorized_fact_condition(scope),
            MemoryFactModel.status == "active",
            MemoryFactModel.review_state != "quarantined",
            or_(
                MemoryFactModel.valid_until.is_(None),
                MemoryFactModel.valid_until > datetime.now(UTC),
            ),
        ]

    async def list(
        self,
        principal: ClientPrincipal,
        *,
        about: Literal["me", "self"],
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        position = decode_cursor(before) if before else None
        if limit < 1 or (
            position is not None
            and (
                not isinstance(position, list)
                or len(position) != 2
                or position[0] != about
                or type(position[1]) is not int
                or position[1] < 1
            )
        ):
            raise ClientError("validation_error")
        async with self.identity.database.read_session() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            conditions = await self._conditions(session, current, about)
            if position:
                conditions.append(MemoryFactModel.id < position[1])
            rows = await self.facts.repository._execute_facts_with_count(
                session, conditions, order_by=(MemoryFactModel.id.desc(),), limit=limit + 1
            )
            facts = await project_memory_fact_rows(session, rows[:limit])
            return {
                "items": [fact_view(fact) for fact in facts],
                "next_cursor": cursor([about, facts[-1].id]) if len(rows) > limit else None,
            }

    async def get(self, principal: ClientPrincipal, fact_id: int) -> dict[str, Any]:
        async with self.identity.database.read_session() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            rows = await self.facts.repository._execute_facts_with_count(
                session,
                [*await self._conditions(session, current, None), MemoryFactModel.id == fact_id],
                order_by=(),
                limit=1,
            )
            if not rows:
                raise ClientError("not_found")
            fact = (await project_memory_fact_rows(session, rows))[0]
            evidence = await session.execute(
                select(
                    ChatEventModel.id,
                    ChatEventModel.canonical_conversation_id,
                    MemoryEvidenceModel.excerpt,
                )
                .join(MemoryEvidenceModel, MemoryEvidenceModel.event_id == ChatEventModel.id)
                .join(MemoryFactModel, MemoryFactModel.id == MemoryEvidenceModel.fact_id)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                )
                .where(
                    MemoryEvidenceModel.fact_id == fact_id,
                    readable_evidence_predicate(),
                    ChatEventModel.suppression_status == "keeper",
                    CanonicalConversationModel.kind == "private",
                    person_family_condition(
                        CanonicalConversationModel.person_id, current.person_id
                    ),
                )
                .order_by(ChatEventModel.id)
            )
            return {
                **fact_view(fact),
                "evidence": [
                    {"event_id": event, "conversation_id": conversation, "excerpt": excerpt}
                    for event, conversation, excerpt in evidence
                ],
            }

    async def _own_fact(
        self, session: AsyncSession, principal: ClientPrincipal, fact_id: int
    ) -> MemoryFact:
        identity = await session.scalar(
            select(MemoryFactModel.id).where(
                MemoryFactModel.id == fact_id,
                MemoryFactModel.scope_type == "person",
                person_family_condition(
                    MemoryFactModel.canonical_subject_person_id, principal.person_id
                ),
            )
        )
        fact = (
            await self.facts.get_fact(identity, session=session) if identity is not None else None
        )
        if fact is None:
            raise ClientError("not_found")
        return fact

    async def _original(
        self,
        session: AsyncSession,
        principal: ClientPrincipal,
        fact_id: int,
        request_id: str,
        bound: str,
    ) -> tuple[dict[str, Any] | None, EventRecord | None]:
        receipt = await personal_command(
            session,
            principal.request_person_id,
            request_id,
            conversation_id=principal.request_conversation_id,
        )
        if receipt is not None:
            if (
                receipt.operation_kind != "client.memory_correction"
                or receipt.operation_ref != str(fact_id)
                or receipt.payload_hash != bound
            ):
                raise ClientError("request_conflict")
            return await self.identity.replay(
                session, principal.device_id, request_id, bound, principal=principal
            ), None
        row = await ClientRequests(self.identity).message(session, principal, request_id)
        event = _event_record(row) if row is not None else None
        if event is not None:
            recorded = next(
                (
                    part["data"]
                    for part in event.segments
                    if part.get("type") == "client_memory_correction"
                ),
                None,
            )
            if (
                event.origin != "memory_correction"
                or recorded is None
                or recorded.get("operation_kind") != "client.memory_correction"
                or recorded.get("target") != str(fact_id)
                or recorded.get("payload_hash") != bound
            ):
                raise ClientError("request_conflict")
        return None, event

    async def _check_revision(self, session: AsyncSession, fact: MemoryFact, expected: str) -> None:
        if fact_signature(fact) != expected:
            replacement = await session.scalar(
                select(MemoryFactModel.id)
                .where(MemoryFactModel.supersedes_id == fact.id)
                .order_by(MemoryFactModel.id.desc())
            )
            raise ClientError(
                "version_conflict",
                details={
                    "current": {**fact_view(fact), "status": fact.status.value},
                    "replacement_fact_id": replacement,
                },
            )

    async def correct(
        self,
        principal: ClientPrincipal,
        fact_id: int,
        request_id: str,
        expected_revision: str,
        action: Literal["correct", "retract"],
        content: str | None,
    ) -> dict[str, Any]:
        content = content.strip() if content is not None else None
        if (action == "correct" and not content) or (action == "retract" and content is not None):
            raise ClientError("validation_error")
        operation = "client.memory_correction"
        payload = {"expected_revision": expected_revision, "action": action, "text": content}
        bound = fingerprint(operation, str(fact_id), payload)
        async with self.identity.database.read_session() as reader:
            current = await self.identity.require_principal(reader, principal)
            if current.platform != "app":
                raise ClientError("validation_error")
            previous, event = await self._original(reader, current, fact_id, request_id, bound)
            if previous is not None:
                return previous
            fact = await self._own_fact(reader, current, fact_id)
            await self._check_revision(reader, fact, expected_revision)
            binding = await reader.get(IdentityBindingModel, current.binding_id)
            assert binding is not None
        if event is None:
            label = (
                f"更正記憶 #{fact_id}：{content}"
                if action == "correct"
                else f"撤回記憶 #{fact_id}：{fact.content}"
            )
            message = self.gateway.message(
                binding,
                request_id,
                label,
                (
                    {"type": "text", "data": {"text": label}},
                    {
                        "type": "client_memory_correction",
                        "data": {
                            "operation_kind": operation,
                            "target": str(fact_id),
                            "payload_hash": bound,
                            **payload,
                        },
                    },
                ),
                conversation_id=current.conversation_id,
            )
            ingress = self.gateway.processor._canonical_ingress
            unit = self.gateway.processor._canonical_uow
            admitted = await ingress.pre_admit(self.gateway.handle, message)
            if admitted.dropped:
                raise ClientError("unauthenticated")
            preparation = await unit.prepare_inbound(
                admitted.message, admitted, origin="memory_correction"
            )
            # This is the actual user management action, recorded once without a Main turn.
            async with self.identity.database.immediate_session() as writer:
                current = await self.identity.require_principal(writer, principal)
                previous, event = await self._original(writer, current, fact_id, request_id, bound)
                if previous is not None:
                    return previous
                if event is None:
                    event = (
                        await unit.append_inbound(
                            admitted.message,
                            admitted,
                            session=writer,
                            origin="memory_correction",
                            prepared=preparation,
                        )
                    ).event
        assert event is not None
        context = MemoryMutationContext(
            event=event,
            conversation_key=f"conversation:{event.canonical_conversation_id}",
            turn_origin="memory_correction",
            delegation_mode="client_memory_correction",
            trigger_actor_user_id=event.sender_user_id,
            decision_actor_type=MemoryDecisionActorType.COMMAND,
            decision_actor_id=current.person_id,
            executed_by_bot_user_id=event.bot_user_id,
            actor_person_id=event.author_person_id,
            operation_id=f"{operation}:{current.request_scope}:{request_id}",
        )
        request = MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT
            if action == "correct"
            else MemoryMutationOperation.INVALIDATE,
            fact_id=fact_id,
            new_content=content,
            reason="personal_memory_correction",
            evidence_quote=content if action == "correct" else event.evidence_content,
        )
        try:
            plan = await self.mutations.prepare_mutation(request, context)
            async with self.identity.database.immediate_session() as writer:
                current = await self.identity.require_principal(writer, principal)
                previous, _ = await self._original(writer, current, fact_id, request_id, bound)
                if previous is not None:
                    return previous
                fact = await self._own_fact(writer, current, fact_id)
                await self._check_revision(writer, fact, expected_revision)
                result = await self.mutations.apply_mutation(plan, session=writer)
                if not result.ok:
                    raise ClientError("precondition_failed", details={"reason": result.reason_code})
                response: dict[str, Any] = {
                    "event_id": event.id,
                    "old_fact_id": result.old_fact_id,
                    "applied_operation": result.applied_operation.value,
                }
                if result.new_fact_id is not None:
                    updated = await self.facts.get_fact(result.new_fact_id, session=writer)
                    assert updated is not None
                    response.update(new_fact_id=updated.id, revision=fact_signature(updated))
                await self.identity.receipts.record_personal_command(
                    writer,
                    principal_id=current.device_id,
                    principal_kind="device",
                    person_id=current.person_id,
                    request_id=request_id,
                    operation=operation,
                    operation_ref=str(fact_id),
                    payload_hash=bound,
                    resource_id=str(event.id),
                    result=response,
                    request_person_id=current.request_person_id,
                    request_conversation_id=current.request_conversation_id,
                )
        except MemoryMutationRejected as error:
            raise ClientError(
                "precondition_failed", details={"reason": error.reason_code}
            ) from error
        except MemoryPartitionResolutionError as error:
            raise ClientError("precondition_failed", details={"reason": str(error)}) from error
        await self.mutations.after_commit(result)
        return {**response, "replay": False}
