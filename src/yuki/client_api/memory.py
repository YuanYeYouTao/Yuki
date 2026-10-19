"""Personal Memory views and atomic commands through the original mutation service."""

from collections.abc import Mapping
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.work_projection import cursor, decode_cursor
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.enums import MemoryScopeType
from yuki.memory.models import MemoryFact
from yuki.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
)
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.read_scope import MemoryReadScopeResolver
from yuki.memory.repository import readable_evidence_predicate
from yuki.memory.service import MemoryFactService
from yuki.memory.signature import fact_signature
from yuki.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel


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
    }


class ClientMemory:
    def __init__(
        self,
        identity: ClientIdentityService,
        gateways: Mapping[str, AppGateway],
        facts: MemoryFactService,
        mutations: MemoryMutationService,
    ) -> None:
        self.identity, self.gateways = identity, gateways
        self.facts, self.mutations = facts, mutations
        self.scopes = MemoryReadScopeResolver(identity.database)

    async def _scope(
        self, session: AsyncSession, principal: ClientPrincipal, about: Literal["me", "self"]
    ) -> AuthorizedMemoryScope:
        scope = await self.scopes.authorized(
            principal.person_id,
            private_scene=True,
            allowed_scopes=(MemoryScopeType.PERSON if about == "me" else MemoryScopeType.SELF,),
            session=session,
        )
        if about == "me":
            own = await self.scopes.person(
                principal.person_id,
                principal.person_id,
                include_person_groups=False,
                session=session,
            )
            scope = scope.model_copy(update={"resolved_targets": own.targets})
        return scope

    async def list(
        self,
        principal: ClientPrincipal,
        *,
        about: Literal["me", "self"],
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        position = decode_cursor(before) if before else None
        if position is not None and (
            not isinstance(position, list)
            or len(position) != 2
            or position[0] != about
            or type(position[1]) is not int
            or position[1] < 1
        ):
            raise ClientError("validation_error")
        async with self.identity.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            scope = await self._scope(session, principal, about)
            rows = await self.facts.repository.list_active_authorized(
                scope,
                before_id=position[1] if position else None,
                limit=limit + 1,
                session=session,
            )
            selected = rows[:limit]
            return {
                "items": [fact_view(fact) for fact in selected],
                "next_cursor": cursor([about, selected[-1].id]) if len(rows) > limit else None,
            }

    async def get(self, principal: ClientPrincipal, fact_id: int) -> dict[str, Any]:
        async with self.identity.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            selected: tuple[MemoryFact, ...] = ()
            for about in ("me", "self"):
                scope = await self._scope(session, principal, about)
                selected = await self.facts.repository.list_active_authorized(
                    scope,
                    fact_id=fact_id,
                    limit=1,
                    session=session,
                )
                if selected:
                    break
            if not selected:
                raise ClientError("not_found")
            # Fact ACL and source ACL are independent. Only the person's original
            # private Conversations expose evidence; deleted events never shift a link.
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
                        CanonicalConversationModel.person_id, principal.person_id
                    ),
                )
                .order_by(ChatEventModel.id)
            )
            return {
                **fact_view(selected[0]),
                "evidence": [
                    {"event_id": event, "conversation_id": conversation, "excerpt": excerpt}
                    for event, conversation, excerpt in evidence
                ],
            }

    async def _own_fact(
        self, session: AsyncSession, principal: ClientPrincipal, fact_id: int
    ) -> MemoryFact:
        own = await session.scalar(
            select(MemoryFactModel.id).where(
                MemoryFactModel.id == fact_id,
                MemoryFactModel.scope_type == MemoryScopeType.PERSON.value,
                person_family_condition(
                    MemoryFactModel.canonical_subject_person_id, principal.person_id
                ),
            )
        )
        fact = await self.facts.get_fact(own, session=session) if own is not None else None
        if fact is None:
            raise ClientError("not_found")
        return fact

    async def correct(
        self,
        principal: ClientPrincipal,
        fact_id: int,
        request_id: str,
        expected_revision: str,
        action: Literal["retract", "correct"],
        content: str | None,
    ) -> dict[str, Any]:
        content = content.strip() if content is not None else None
        if (action == "correct" and not content) or (action == "retract" and content is not None):
            raise ClientError("validation_error")
        operation = "client.memory_correction"
        bound = fingerprint(
            operation,
            str(fact_id),
            {
                "expected_revision": expected_revision,
                "action": action,
                "text": content,
            },
        )
        # Keep the explicitly selected original App entry and its request namespace.
        async with self.identity.database.sessions() as reader:
            principal = await self.identity.require_principal(reader, principal)
            if principal.platform != "app":
                principal = await self.identity.require_device(reader, principal.device_id)
            previous = await self.identity.replay(
                reader,
                principal.device_id,
                request_id,
                bound,
                principal=principal,
            )
            if previous is not None:
                return previous
            fact = await self._own_fact(reader, principal, fact_id)
            binding = await reader.get(IdentityBindingModel, principal.binding_id)
            assert binding is not None
        label = (
            f"更正記憶 #{fact_id}：{content}"
            if action == "correct"
            else f"撤回記憶 #{fact_id}：{fact.content}"
        )
        gateway = self.gateways["app"]
        message = gateway.message(
            binding, request_id, label, ({"type": "text", "data": {"text": label}},)
        )
        admitted = await gateway.processor._canonical_ingress.pre_admit(gateway.handle, message)
        if admitted.dropped:
            raise ClientError("unauthenticated")

        async def apply(session: AsyncSession) -> dict[str, Any]:
            current = await self.identity.require_principal(session, principal)
            previous = await self.identity.replay(
                session,
                current.device_id,
                request_id,
                bound,
                principal=current,
            )
            if previous is not None:
                return previous
            fact = await self._own_fact(session, current, fact_id)
            if fact_signature(fact) != expected_revision:
                replacement = await session.scalar(
                    select(MemoryFactModel.id)
                    .where(MemoryFactModel.supersedes_id == fact_id)
                    .order_by(MemoryFactModel.id.desc())
                )
                raise ClientError(
                    "version_conflict",
                    details={
                        "current": {**fact_view(fact), "status": fact.status.value},
                        "replacement_fact_id": replacement,
                    },
                )
            await self.facts.prepare_evidence_write((fact_id,), session=session, targets=(fact,))
            if action == "correct":
                await self.facts.prepare_evidence_copy((fact_id,), fact, session=session)
            appended = await gateway.processor._canonical_uow.append_inbound(
                admitted.message,
                admitted,
                session=session,
                origin="memory_correction",
            )
            event = appended.event
            result = await self.mutations.mutate(
                MemoryMutationRequest(
                    operation=MemoryMutationOperation.CORRECT
                    if action == "correct"
                    else MemoryMutationOperation.INVALIDATE,
                    fact_id=fact_id,
                    new_content=content,
                    reason="personal_memory_correction",
                ),
                MemoryMutationContext(
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
                ),
                session=session,
            )
            if not result.ok:
                raise ClientError("precondition_failed", details={"reason": result.reason_code})
            response: dict[str, Any] = {
                "event_id": event.id,
                "old_fact_id": result.old_fact_id,
                "applied_operation": result.applied_operation.value,
            }
            if result.new_fact_id is not None:
                updated = await self.facts.get_fact(result.new_fact_id, session=session)
                assert updated is not None
                response.update(new_fact_id=updated.id, revision=fact_signature(updated))
            await self.identity.receipts.record_personal_command(
                session,
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
            )
            return {**response, "replay": False}

        response = await self.facts.repository.apply_evidence_write(apply)
        if response.get("new_fact_id") is not None and not response.get("replay"):
            await self.facts.schedule_embedding(response["new_fact_id"])
        return response
