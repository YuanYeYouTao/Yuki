"""Small management projections over original domain facts."""

from __future__ import annotations

import zlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, literal_column, select

from yuki.admin.authority.operations import OperationKind, OperationRef
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.config_files import ConfigFileService
from yuki.admin.operations.base import authorize_current, fail
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.identity import PrincipalId, RequestId
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import (
    current_person_id,
    current_person_ids,
    person_family_condition,
)
from yuki.memory.dream.db_models import MemoryDreamRunModel
from yuki.persistence.command_receipts import control_operation, run_operation
from yuki.persistence.database import Database
from yuki.persistence.models import MemoryRebuildRunModel
from yuki.persistence.unit_of_work import state_revision
from yuki.plugin_host.configuration_service import PluginConfigurationService
from yuki.plugin_host.manager import PluginManager
from yuki.plugin_host.repository import PluginInstallationRecord
from yuki.runtime.work_schema_v1 import work


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


def operation_view(operation: OperationRef) -> dict[str, Any]:
    return {
        **asdict(operation),
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
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
        await authorize_current(self.database, principal, "identity.person.read")
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
                "source_enabled": row.enabled,
                "source_revision": row.revision,
                "revision": current.revision,
                "enabled": current.enabled,
                "display_name": current.display_name,
                "is_superuser": current.is_superuser,
                "bindings": [
                    self._binding_view(binding)
                    for binding in await session.scalars(
                        select(IdentityBindingModel)
                        .where(person_family_condition(IdentityBindingModel.person_id, current.id))
                        .order_by(IdentityBindingModel.id)
                    )
                ],
            }

    async def binding(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        await authorize_current(self.database, principal, "identity.binding.read")
        async with self.database.sessions() as session:
            row = await session.get(IdentityBindingModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            return {
                **self._binding_view(row),
                "current_person_id": await current_person_id(session, row.person_id),
            }

    async def plugins_list(self, principal: ControlPrincipal) -> list[dict[str, Any]]:
        await authorize_current(self.database, principal, "control.plugin.read")
        return [plugin_view(row) for row in await self.plugins.list()]

    async def plugin(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        await authorize_current(self.database, principal, "control.plugin.read")
        row = await self.plugins.show(identity)
        if row is None:
            raise fail(ProblemCode.NOT_FOUND)
        report = await self.plugins.doctor(identity)
        return {**plugin_view(row), **report.model_dump(mode="json")}

    async def plugin_config(
        self, principal: ControlPrincipal, identity: str, scope_type: str, owner_id: str | None
    ) -> dict[str, Any]:
        await authorize_current(self.database, principal, "control.plugin.config.content.read")
        return await PluginConfigurationService(
            self.database, self.plugins.configuration_schema
        ).read(identity, scope_type=scope_type, owner_id=owner_id)

    async def operation(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        await authorize_current(self.database, principal, "control.operation.read")
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
                            **operation_view(control_operation(row)),
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
                        return operation_view(
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
        await authorize_current(self.database, principal, "control.execution.metadata.read")
        if content:
            await authorize_current(self.database, principal, "control.execution.content.read")
        field = {
            "turn": ExecutionTraceEntryModel.turn_id,
            "work": ExecutionTraceEntryModel.work_id,
            "event": ExecutionTraceEntryModel.source_event_id,
        }[kind]
        try:
            value: str | int = int(identity) if kind == "event" else identity
        except ValueError:
            raise fail(ProblemCode.VALIDATION_ERROR) from None
        metadata_fields = (
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
        fields = metadata_fields + (
            ("payload_gzip", "payload_bytes", "payload_sha256") if content else ()
        )
        async with self.database.sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(*(getattr(ExecutionTraceEntryModel, name) for name in fields))
                        .where(
                            field == value,
                            ExecutionTraceEntryModel.id > after,
                            ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
                        )
                        .order_by(ExecutionTraceEntryModel.id)
                        .limit(limit + 1)
                    )
                ).mappings()
            )
            items = []
            for row in rows[:limit]:
                item = {name: row[name] for name in metadata_fields}
                if content:
                    try:
                        item["payload"] = (
                            decode_payload(
                                row["payload_gzip"],
                                size=row["payload_bytes"],
                                digest=row["payload_sha256"] or "",
                            )
                            if row["payload_gzip"] is not None
                            else None
                        )
                    except (ValueError, TypeError, zlib.error):
                        raise fail(ProblemCode.STATE_MISMATCH) from None
                items.append(item)
            return {
                "items": items,
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    @staticmethod
    def _binding_view(row: IdentityBindingModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "person_id": row.person_id,
            "platform": row.platform,
            "external_account_id": row.external_account_id,
            "display_name": row.display_name,
            "status": row.status,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
            "revision": row.revision,
        }

    async def persons(
        self, principal: ControlPrincipal, after: str = "", limit: int = 100
    ) -> dict[str, Any]:
        await authorize_current(self.database, principal, "identity.person.read")
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(CanonicalPersonModel)
                    .where(
                        CanonicalPersonModel.merged_into_person_id.is_(None),
                        CanonicalPersonModel.id > after,
                    )
                    .order_by(CanonicalPersonModel.id)
                    .limit(limit + 1)
                )
            )
            items = [
                {
                    "id": row.id,
                    "display_name": row.display_name,
                    "enabled": row.enabled,
                    "is_superuser": row.is_superuser,
                    "revision": row.revision,
                }
                for row in rows[:limit]
            ]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    async def bindings(
        self,
        principal: ControlPrincipal,
        after: str = "",
        limit: int = 100,
        *,
        person_id: str | None = None,
        platform: str | None = None,
        external_account_id: str | None = None,
    ) -> dict[str, Any]:
        await authorize_current(self.database, principal, "identity.binding.read")
        stmt = select(IdentityBindingModel).where(IdentityBindingModel.id > after)
        if person_id is not None:
            stmt = stmt.where(person_family_condition(IdentityBindingModel.person_id, person_id))
        if platform is not None:
            stmt = stmt.where(IdentityBindingModel.platform == platform)
        if external_account_id is not None:
            stmt = stmt.where(IdentityBindingModel.external_account_id == external_account_id)
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(stmt.order_by(IdentityBindingModel.id).limit(limit + 1))
            )
            roots = await current_person_ids(session, (row.person_id for row in rows[:limit]))
            items = [
                {
                    **self._binding_view(row),
                    "current_person_id": roots[row.person_id],
                }
                for row in rows[:limit]
            ]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    @staticmethod
    def _presence_view(row: PresenceModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "platform": row.platform,
            "external_account_id": row.external_account_id,
            "enabled": row.enabled,
            "status": row.status,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
            "ingest_eligible": row.ingest_eligible,
            "revision": row.revision,
        }

    async def presences(
        self, principal: ControlPrincipal, after: str = "", limit: int = 100
    ) -> dict[str, Any]:
        await authorize_current(self.database, principal, "identity.presence.read")
        async with self.database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(PresenceModel)
                    .where(PresenceModel.id > after)
                    .order_by(PresenceModel.id)
                    .limit(limit + 1)
                )
            )
            items = [self._presence_view(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    async def presence(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        await authorize_current(self.database, principal, "identity.presence.read")
        async with self.database.sessions() as session:
            row = await session.get(PresenceModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            return self._presence_view(row)

    @staticmethod
    def _work_columns() -> tuple[Any, ...]:
        from yuki.runtime.work_management import CLASSIFICATION_KEYS

        return tuple(
            work.c[name]
            for name in (
                "id",
                "parent_work_id",
                "conversation_id",
                "generation",
                "goal",
                "revision",
                "state",
                "reason",
                "model_requests",
                "tool_calls",
                "active_seconds",
                "sent_messages",
                "created",
                "updated",
            )
        ) + tuple(
            func.json_extract(work.c.source_json, literal_column(f"'$.{key}'")).label(key)
            for key in CLASSIFICATION_KEYS
        )

    @staticmethod
    def _work_view(row: Any) -> dict[str, Any]:
        from yuki.runtime.work_management import CLASSIFICATION_KEYS

        view = dict(row)
        view["source"] = {key: view.pop(key) for key in CLASSIFICATION_KEYS}
        return view

    async def works(
        self,
        principal: ControlPrincipal,
        after: str = "",
        limit: int = 100,
        *,
        state: str | None = None,
    ) -> dict[str, Any]:
        await authorize_current(self.database, principal, "control.work.read")
        stmt = select(*self._work_columns()).where(work.c.id > after)
        if state is not None:
            stmt = stmt.where(work.c.state == state)
        async with self.database.sessions() as session:
            rows = list(
                (await session.execute(stmt.order_by(work.c.id).limit(limit + 1))).mappings()
            )
        items = [self._work_view(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    async def work(self, principal: ControlPrincipal, identity: str) -> dict[str, Any]:
        from yuki.runtime.work_management import management_view

        await authorize_current(self.database, principal, "control.work.read")
        async with self.database.sessions() as session:
            row = (
                (await session.execute(select(*self._work_columns()).where(work.c.id == identity)))
                .mappings()
                .first()
            )
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            lifecycle = await management_view(session, identity)
        assert lifecycle is not None
        return {**self._work_view(row), **lifecycle}
