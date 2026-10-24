"""A Person's explicit proactive destination, using the canonical active route."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.conversation.canonical_db_models import CanonicalConversationModel, PersonActiveRouteModel
from yuki.identity.db_models import IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import person_family_condition


async def route_view(session: AsyncSession, person_id: str) -> dict[str, Any]:
    route = await session.get(PersonActiveRouteModel, person_id)
    binding = await session.get(IdentityBindingModel, route.identity_binding_id) if route else None
    return {
        "binding_id": binding.id if binding else None,
        "platform": binding.platform if binding else None,
        "paused": route.paused if route else True,
        "revision": route.revision if route else 0,
    }


class ClientProactiveRoute:
    def __init__(self, identity: ClientIdentityService) -> None:
        self.identity = identity

    async def get(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.identity.database.read_session() as session:
            current = await self.identity.require_principal(session, principal)
            return await route_view(session, current.person_id)

    async def set(
        self,
        principal: ClientPrincipal,
        request_id: str,
        expected_revision: int,
        binding_id: str,
    ) -> dict[str, Any]:
        async with self.identity.database.immediate_session() as session:
            current = await self.identity.require_principal(session, principal)
            operation = "client.proactive_route"
            bound = fingerprint(
                operation,
                current.request_person_id,
                {
                    "expected_revision": expected_revision,
                    "binding_id": binding_id,
                },
            )
            old = await self.identity.replay(
                session,
                current.device_id,
                request_id,
                bound,
                principal=current,
            )
            if old is not None:
                return old
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.id == binding_id,
                    person_family_condition(IdentityBindingModel.person_id, current.person_id),
                    IdentityBindingModel.status == "active",
                )
            )
            if binding is None:
                raise ClientError("not_found")
            route = await session.get(PersonActiveRouteModel, current.person_id)
            if (route.revision if route else 0) != expected_revision:
                raise ClientError(
                    "version_conflict",
                    details={
                        "current": await route_view(session, current.person_id),
                    },
                )
            candidates = list(
                await session.scalars(
                    select(PresenceModel.id)
                    .distinct()
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.entry_presence_id == PresenceModel.id,
                    )
                    .where(
                        CanonicalConversationModel.identity_binding_id == binding.id,
                        PresenceModel.platform == binding.platform,
                        PresenceModel.enabled.is_(True),
                    )
                )
            )
            if (
                route
                and route.identity_binding_id == binding_id
                and route.presence_id in candidates
            ):
                presence_id = route.presence_id
            elif len(candidates) == 1:
                presence_id = candidates[0]
            else:
                raise ClientError(
                    "precondition_failed",
                    details={"reason": "route_ambiguous" if candidates else "route_unavailable"},
                )
            now = datetime.now(UTC)
            if route is None:
                route = PersonActiveRouteModel(
                    person_id=current.person_id,
                    identity_binding_id=binding_id,
                    presence_id=presence_id,
                    route_generation=1,
                    revision=1,
                    paused=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(route)
            else:
                route.identity_binding_id, route.presence_id, route.paused = (
                    binding_id,
                    presence_id,
                    False,
                )
                route.route_generation += 1
                route.revision += 1
                route.updated_at = now
            await session.flush()
            result = await route_view(session, current.person_id)
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=current.device_id,
                principal_kind="device",
                person_id=current.person_id,
                request_id=request_id,
                operation=operation,
                operation_ref=current.request_person_id,
                payload_hash=bound,
                resource_id=binding_id,
                result=result,
                request_person_id=current.request_person_id,
                request_conversation_id=current.request_conversation_id,
            )
            return result
