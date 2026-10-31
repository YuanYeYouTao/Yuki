"""Personal lifecycle commands compose current automation rules and original receipts."""

import hashlib
import json
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.models import AutomationRecord
from yuki.automation.repository import (
    AutomationRepository,
    _automation_record,
    _definition_statement,
)
from yuki.automation.service import AutomationService
from yuki.automation.task_spec import TaskSpec
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.domain.tool_actor import ToolActor
from yuki.identity.db_models import PresenceModel
from yuki.identity.person_identity import same_person_id
from yuki.persistence.models import AutomationModel
from yuki.runtime.origin import TurnOrigin


def automation_revision(row: AutomationRecord) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "version_id": row.version_id,
                "status": row.status.value,
                "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                "updated_at": row.updated_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def automation_view(row: AutomationRecord) -> dict[str, Any]:
    schedule = row.schedule
    timezone = getattr(schedule, "timezone", None) or row.timezone
    match schedule.type:
        case "after":
            description = f"建立後 {schedule.seconds} 秒"
        case "interval":
            description = f"每 {schedule.seconds} 秒"
        case "once":
            description = f"{schedule.local_datetime.isoformat()}（{timezone}）"
        case "daily":
            description = f"每天 {schedule.hour:02d}:{schedule.minute:02d}（{timezone}）"
        case "weekly":
            days = "、".join("一二三四五六日"[day - 1] for day in schedule.weekdays)
            description = f"每週{days} {schedule.hour:02d}:{schedule.minute:02d}（{timezone}）"
    return {
        "automation_id": row.id,
        "title": row.name,
        "schedule_text": description,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "state": row.status.value,
        "revision": automation_revision(row),
        "conversation_id": row.canonical_conversation_id,
    }


class ClientAutomations:
    def __init__(
        self,
        identity: ClientIdentityService,
        repository: AutomationRepository,
        service: AutomationService,
    ) -> None:
        self.identity, self.repository, self.service = identity, repository, service

    async def list(
        self, principal: ClientPrincipal, scope: Literal["mine", "self"]
    ) -> dict[str, Any]:
        async with self.identity.database.read_session() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            if scope == "self":
                await self.identity.require_superuser(session, current)
            rows = await session.execute(
                _definition_statement()
                .where(
                    self.repository._owner_clause("self" if scope == "self" else current.person_id)
                )
                .order_by(AutomationModel.updated_at.desc(), AutomationModel.id.desc())
            )
            return {"items": [automation_view(_automation_record(*row)) for row in rows]}

    async def _mine(
        self, session: AsyncSession, principal: ClientPrincipal, automation_id: int
    ) -> AutomationRecord:
        row = await self.repository.get(automation_id, session=session)
        if (
            row is None
            or row.creator_kind != "person"
            or not row.canonical_creator_person_id
            or not await same_person_id(
                session, row.canonical_creator_person_id, principal.person_id
            )
        ):
            raise ClientError("not_found")
        return row

    async def action(
        self,
        principal: ClientPrincipal,
        automation_id: int,
        request_id: str,
        expected_revision: str,
        action: Literal["pause", "resume", "delete"],
    ) -> dict[str, Any]:
        operation = "client.automation"
        bound = fingerprint(
            operation,
            str(automation_id),
            {"expected_revision": expected_revision, "action": action},
        )
        async with self.identity.database.read_session() as reader:
            current = await self.identity.require_principal(reader, principal)
            previous = await self.identity.replay(
                reader, current.device_id, request_id, bound, principal=current
            )
            if previous is not None:
                return previous
            prepared = await self._mine(reader, current, automation_id)
            if action == "resume":
                TaskSpec.model_validate(prepared.script)
            presence = await reader.get(PresenceModel, current.presence_id)
            assert presence is not None
            actor = ToolActor(
                user_id=current.external_account_id,
                bot_user_id=presence.external_account_id,
                group_id=None,
                origin=TurnOrigin.USER_MESSAGE,
                instruction=action,
                execution_id=f"{operation}:{current.request_scope}:{request_id}",
                person_id=current.source_person_id,
                conversation_id=current.conversation_id,
                presence_id=current.presence_id,
                platform=current.platform,
            )
        async with self.identity.database.immediate_session() as session:
            current = await self.identity.require_principal(session, principal)
            previous = await self.identity.replay(
                session, current.device_id, request_id, bound, principal=current
            )
            if previous is not None:
                return previous
            row = await self._mine(session, current, automation_id)
            if automation_revision(row) != expected_revision:
                raise ClientError("version_conflict", details={"current": automation_view(row)})
            try:
                changed = await self.service.transition_status(
                    automation_id,
                    "cancel" if action == "delete" else action,
                    actor=actor,
                    conversation_key=f"conversation:{current.conversation_id}",
                    session=session,
                )
                if not changed and action != "delete":
                    raise ValueError("automation state does not allow this action")
            except (ValueError, PermissionError) as error:
                raise ClientError(
                    "precondition_failed", details={"state": row.status.value}
                ) from error
            updated = await self.repository.get(automation_id, session=session)
            assert updated is not None
            response = automation_view(updated)
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=current.device_id,
                principal_kind="device",
                person_id=current.person_id,
                request_id=request_id,
                operation=operation,
                operation_ref=str(automation_id),
                payload_hash=bound,
                resource_id=str(automation_id),
                result=response,
                request_person_id=current.request_person_id,
                request_conversation_id=current.request_conversation_id,
            )
            return {**response, "replay": False}
