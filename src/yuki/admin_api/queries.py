"""Small management projections over original domain facts."""

from __future__ import annotations

import zlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select

from yuki.admin.authority.operations import OperationKind
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.config_files import ConfigFileService
from yuki.admin_api.commands import authorize, fail
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.identity import PrincipalId, RequestId
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.person_identity import current_person_id
from yuki.memory.dream.db_models import MemoryDreamRunModel
from yuki.persistence.command_receipts import control_operation, run_operation
from yuki.persistence.database import Database
from yuki.persistence.models import MemoryRebuildRunModel
from yuki.persistence.unit_of_work import state_revision
from yuki.plugin_host.configuration_service import PluginConfigurationService
from yuki.plugin_host.manager import PluginManager
from yuki.plugin_host.repository import PluginInstallationRecord


def plugin_view(record: PluginInstallationRecord) -> dict[str, Any]:
    return {
        "plugin_id": record.plugin_id,
        "name": record.name,
        "version": record.version,
        "status": record.status,
        "enabled": record.enabled,
        "revision": state_revision(record.updated_at),
        "manifest_hash": record.manifest_hash,
        "requested_permissions": list(record.requested_permissions),
        "approved_permissions": list(record.approved_permissions),
        "error_category": record.last_error_category,
    }


class AdminQueries:
    def __init__(
        self,
        database: Database,
        plugins: PluginManager,
        files: ConfigFileService,
        health: Callable[[], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        self.database, self.plugins, self.files, self.health = database, plugins, files, health

    async def person(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        authorize(principal, "identity.person.read")
        async with self.database.sessions() as session:
            row = await session.get(CanonicalPersonModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            root = await current_person_id(session, row.id)
            current = await session.get(CanonicalPersonModel, root)
            assert current is not None
            return {
                "id": root,
                "source_person_id": row.id,
                "revision": current.revision,
                "enabled": current.enabled,
                "display_name": current.display_name,
            }

    async def binding(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        authorize(principal, "identity.binding.read")
        async with self.database.sessions() as session:
            row = await session.get(IdentityBindingModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            return {
                "id": row.id,
                "person_id": row.person_id,
                "platform": row.platform,
                "status": row.status,
                "revision": row.revision,
            }

    async def plugins_list(self, principal: ControlPrincipal) -> list[dict[str, Any]]:
        authorize(principal, "control.plugin.read")
        return [plugin_view(row) for row in await self.plugins.list()]

    async def plugin(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        authorize(principal, "control.plugin.read")
        row = await self.plugins.show(identity)
        if row is None:
            raise fail(ProblemCode.NOT_FOUND)
        report = await self.plugins.doctor(identity)
        return {
            **plugin_view(row),
            "manifest_hash_matches": report.manifest_hash_matches,
            "manifest_available": report.manifest_available,
        }

    async def plugin_config(
        self, principal: ControlPrincipal, identity: str, scope_type: str, owner_id: str | None
    ) -> dict[str, Any]:
        authorize(principal, "control.plugin.config.content.read")
        return await PluginConfigurationService(
            self.database, self.plugins.configuration_schema
        ).read(identity, scope_type=scope_type, owner_id=owner_id)

    async def operation(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        authorize(principal, "control.operation.read")
        parts = identity.split(":")
        try:
            if len(parts) == 1:
                parts = ["control", principal.principal_id.text, RequestId.parse(identity).text]
            async with self.database.sessions() as session:
                if len(parts) == 3 and parts[0] == "control":
                    actor, request = PrincipalId.parse(parts[1]), RequestId.parse(parts[2])
                    row = await session.scalar(
                        select(ControlCommandReceiptModel).where(
                            ControlCommandReceiptModel.principal_id == actor.text,
                            ControlCommandReceiptModel.request_id == request.text,
                        )
                    )
                    if row is not None:
                        # Legacy control requests keep their original result and status.
                        return {
                            **asdict(control_operation(row)),
                            "result_resource_id": row.result_resource_id,
                            "result_revision": row.result_revision,
                        }
                elif len(parts) == 2 and parts[0] in {"rebuild", "dream"}:
                    public_id = RequestId.parse(parts[1]).text
                    model = MemoryRebuildRunModel if parts[0] == "rebuild" else MemoryDreamRunModel
                    run = (
                        await session.execute(
                            select(
                                model.public_id,
                                model.status,
                                model.created_at,
                                model.updated_at,
                                model.error_category,
                            ).where(model.public_id == public_id)
                        )
                    ).one_or_none()
                    if run is not None:
                        return asdict(
                            run_operation(
                                OperationKind(parts[0]),
                                run.public_id,
                                run.status,
                                created_at=run.created_at,
                                updated_at=run.updated_at,
                                error_category=run.error_category,
                            )
                        )
                else:
                    raise ValueError("invalid operation ID")
        except ValueError:
            raise fail(ProblemCode.VALIDATION_ERROR) from None
        raise fail(ProblemCode.NOT_FOUND)

    async def traces(
        self,
        principal: ControlPrincipal,
        kind: Literal["turn", "work", "event"],
        identity: str,
        after: int,
        limit: int,
        content: bool,
    ) -> dict[str, Any]:
        authorize(principal, "control.execution.metadata.read")
        if content:
            authorize(principal, "control.execution.content.read")
        field = {
            "turn": ExecutionTraceEntryModel.turn_id,
            "work": ExecutionTraceEntryModel.work_id,
            "event": ExecutionTraceEntryModel.source_event_id,
        }[kind]
        try:
            value: str | int = int(identity) if kind == "event" else identity
        except ValueError:
            raise fail(ProblemCode.VALIDATION_ERROR) from None
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ExecutionTraceEntryModel)
                    .where(
                        field == value,
                        ExecutionTraceEntryModel.id > after,
                        ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
                    )
                    .order_by(ExecutionTraceEntryModel.id)
                    .limit(limit + 1)
                )
            )
            items = []
            for row in rows[:limit]:
                item = {
                    name: getattr(row, name)
                    for name in (
                        "id",
                        "conversation_id",
                        "turn_id",
                        "work_id",
                        "operation_id",
                        "source_event_id",
                        "delivered_event_id",
                        "kind",
                        "origin",
                        "generation",
                        "created_at",
                        "expires_at",
                        "payload_status",
                    )
                }
                if content:
                    try:
                        item["payload"] = (
                            decode_payload(
                                row.payload_gzip,
                                size=row.payload_bytes,
                                digest=row.payload_sha256 or "",
                            )
                            if row.payload_gzip is not None
                            else None
                        )
                    except (ValueError, TypeError, zlib.error):
                        raise fail(ProblemCode.STATE_MISMATCH) from None
                items.append(item)
            return {
                "items": items,
                "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
            }
