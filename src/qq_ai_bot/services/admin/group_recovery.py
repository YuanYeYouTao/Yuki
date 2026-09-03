"""Explicit, audited recovery of a known Space; never an automatic unpause."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.audit import add_audit_event
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.conversation.canonical_db_models import (
    ControlCommandReceiptModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.services.admin.control_auth import require_capability
from qq_ai_bot.services.admin.group_admin import SpaceAdminContext


class GroupRecoveryService:
    def __init__(
        self, database: Database, registry: GatewayConnectionRegistry, router: PresenceRouter
    ) -> None:
        self._database = database
        self._registry = registry
        self._router = router

    async def enable(
        self,
        context: SpaceAdminContext,
        *,
        presence_id: str,
        connection_id: str,
        audit: ControlAuditRef,
        event_type: str,
    ) -> bool:
        """Return False on a replay. No model, event append, generation reset or new owner."""

        require_capability(context, "control.group.mutate")
        target = context.canonical_target
        if target.space_id is None:
            raise RouteSendError("none")
        space_id = target.space_id.text
        principal_id = context.principal.principal_id.text
        if context.principal.person_id is None:
            raise PermissionError("invalid_control_principal")
        person_id = context.principal.person_id.text
        payload_hash = hashlib.sha256(
            json.dumps(
                [
                    "group.recover",
                    space_id,
                    target.storage_group_id,
                    presence_id,
                    event_type,
                    audit.trigger_message_id,
                    principal_id,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._database.sessions() as session:
            await self._require_actor(session, person_id, audit)
            presence = await session.get(PresenceModel, presence_id)
            if (
                presence is None
                or not presence.enabled
                or not presence.ingest_eligible
                or presence.platform != "qq"
                or presence.external_account_id != audit.bot_user_id
            ):
                raise RouteSendError("none")
            if await self._replayed(session, principal_id, payload_hash):
                return False
            binding = await session.scalar(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.space_id == space_id,
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.external_space_id == target.storage_group_id,
                )
            )
            if binding is None:
                raise RouteSendError("none")
            binding_id = binding.id
            before = await self._state(session, space_id, binding_id)
            presences = list(await session.scalars(select(PresenceModel)))
        connections_before = self._connections(presences)
        candidate = await self._router.group_recovery_candidate(binding_id)
        if candidate.presence_id != presence_id:
            raise RouteSendError("not_ingest")
        preserve_send = await self._router.space_send_pin_healthy(space_id)
        async with self._database.immediate_session() as session:
            await self._require_actor(session, person_id, audit)
            if await self._replayed(session, principal_id, payload_hash):
                return False
            if before != await self._state(session, space_id, binding_id):
                raise RouteSendError("conflict")
            current_presences = list(await session.scalars(select(PresenceModel)))
            if connections_before != self._connections(current_presences):
                raise RouteSendError("conflict")
            connection = self._registry.resolve_active(presence_id)
            if (
                connection.snapshot.connection_id != connection_id
                or connection.snapshot.platform != presence.platform
                or connection.snapshot.external_account_id != presence.external_account_id
            ):
                raise RouteSendError("conflict")
            space = await session.get(CanonicalSpaceModel, space_id)
            assert space is not None  # Included in the revision snapshot.
            now = datetime.now(UTC)
            if not space.enabled:
                space.enabled = True
                space.revision += 1
                space.updated_at = now
            ingest = await session.get(SpaceBindingIngestRouteModel, binding_id)
            if ingest is None:
                session.add(
                    SpaceBindingIngestRouteModel(
                        space_binding_id=binding_id,
                        ingest_presence_id=presence_id,
                        route_generation=1,
                        revision=1,
                        paused=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif ingest.paused or ingest.ingest_presence_id != presence_id:
                ingest.ingest_presence_id = presence_id
                ingest.paused = False
                ingest.route_generation += 1
                ingest.revision += 1
                ingest.updated_at = now
            route = await session.get(SpaceActiveRouteModel, space_id)
            if route is None:
                session.add(
                    SpaceActiveRouteModel(
                        space_id=space_id,
                        space_binding_id=binding_id,
                        presence_id=presence_id,
                        route_generation=1,
                        revision=1,
                        paused=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif not preserve_send and (
                route.paused
                or route.space_binding_id != binding_id
                or route.presence_id != presence_id
            ):
                route.space_binding_id = binding_id
                route.presence_id = presence_id
                route.paused = False
                route.route_generation += 1
                route.revision += 1
                route.updated_at = now
            await session.flush()
            after = await self._state(session, space_id, binding_id)
            event = await add_audit_event(
                session,
                actor=audit,
                capability="control.group.mutate",
                operation="recover",
                target_type="space",
                target_id=space_id,
                before=before,
                after=after,
                success=True,
                error_category=None,
                duration_seconds=0,
            )
            session.add(
                ControlCommandReceiptModel(
                    principal_id=principal_id,
                    request_id=context.request_id.text,
                    payload_hash=payload_hash,
                    status="succeeded",
                    result_resource_id=space_id,
                    result_revision=space.revision,
                    effective_state_json=json.dumps({"enabled": True}),
                    audit_id=event.id,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            if connections_before != self._connections(current_presences):
                raise RouteSendError("conflict")
        return True

    @staticmethod
    async def _require_actor(session: AsyncSession, person_id: str, audit: ControlAuditRef) -> None:
        actor = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "qq",
                IdentityBindingModel.external_account_id == audit.user_id,
            )
        )
        person = await session.get(CanonicalPersonModel, person_id)
        yuki = await session.scalar(
            select(PresenceModel.id).where(
                PresenceModel.platform == "qq",
                PresenceModel.external_account_id == audit.user_id,
            )
        )
        if (
            actor is None
            or actor.status != "active"
            or actor.person_id != person_id
            or person is None
            or not person.enabled
            or yuki is not None
        ):
            raise PermissionError("invalid_control_principal")

    @staticmethod
    async def _replayed(session: AsyncSession, principal_id: str, payload_hash: str) -> bool:
        return (
            await session.scalar(
                select(ControlCommandReceiptModel.id).where(
                    ControlCommandReceiptModel.principal_id == principal_id,
                    ControlCommandReceiptModel.payload_hash == payload_hash,
                )
            )
            is not None
        )

    @staticmethod
    async def _state(session: AsyncSession, space_id: str, binding_id: str) -> dict[str, object]:
        space = await session.get(CanonicalSpaceModel, space_id)
        binding = await session.get(SpaceBindingModel, binding_id)
        if space is None or binding is None or binding.status != "active":
            raise RouteSendError("none")
        ingest = await session.get(SpaceBindingIngestRouteModel, binding_id)
        route = await session.get(SpaceActiveRouteModel, space_id)
        return {
            "space": [space.revision, bool(space.enabled)],
            "binding": [binding.revision, binding.status, binding.space_id],
            "ingest": None
            if ingest is None
            else [
                ingest.revision,
                ingest.route_generation,
                ingest.ingest_presence_id,
                ingest.paused,
            ],
            "send": None
            if route is None
            else [
                route.revision,
                route.route_generation,
                route.space_binding_id,
                route.presence_id,
                route.paused,
            ],
        }

    def _connections(self, presences: list[PresenceModel]) -> list[tuple[object, ...]]:
        result: list[tuple[object, ...]] = []
        for presence in sorted(presences, key=lambda row: row.id):
            try:
                connection = self._registry.resolve_active(presence.id).snapshot
                token = connection.connection_id
            except RegistryClosed as exc:
                token = exc.category
            result.append(
                (presence.id, presence.revision, presence.enabled, presence.ingest_eligible, token)
            )
        return result
