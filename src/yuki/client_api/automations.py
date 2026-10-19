"""Personal views and CAS commands over the existing automation domain."""

import hashlib
import json
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.models import AutomationRecord, AutomationStatus
from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.identity.person_identity import same_person_id


def automation_revision(row: AutomationRecord) -> str:
    payload = {
        "version_id": row.version_id,
        "status": row.status.value,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "updated_at": row.updated_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def schedule_text(row: AutomationRecord) -> str:
    schedule = row.schedule
    timezone = getattr(schedule, "timezone", None) or row.timezone
    match schedule.type:
        case "after":
            return f"建立後 {schedule.seconds} 秒"
        case "interval":
            return f"每 {schedule.seconds} 秒"
        case "once":
            return f"{schedule.local_datetime.isoformat()}（{timezone}）"
        case "daily":
            return f"每天 {schedule.hour:02d}:{schedule.minute:02d}（{timezone}）"
        case "weekly":
            days = "、".join("一二三四五六日"[day - 1] for day in schedule.weekdays)
            return f"每週{days} {schedule.hour:02d}:{schedule.minute:02d}（{timezone}）"


def automation_view(row: AutomationRecord) -> dict[str, Any]:
    return {
        "automation_id": row.id,
        "title": row.name,
        "schedule_text": schedule_text(row),
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "state": row.status.value,
        "revision": automation_revision(row),
        "conversation_id": row.authority_snapshot.get("canonical_conversation_id"),
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
        async with self.identity.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            if scope == "self" and not principal.is_owner:
                raise ClientError("owner_required")
            rows = await self.repository.list_owner_snapshot(
                "self" if scope == "self" else principal.person_id,
                session=session,
            )
            return {"items": [automation_view(row) for row in rows]}

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
            {
                "expected_revision": expected_revision,
                "action": action,
            },
        )
        async with self.identity.database.immediate_session() as session:
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
            row = await self._mine(session, current, automation_id)
            if automation_revision(row) != expected_revision:
                raise ClientError("version_conflict", details={"current": automation_view(row)})
            if row.status is AutomationStatus.CANCELLED:
                raise ClientError("precondition_failed", details={"state": row.status.value})
            try:
                updated = await self.service.administer_transition(
                    automation_id,
                    action="cancel" if action == "delete" else action,
                    session=session,
                )
            except ValueError as exc:
                raise ClientError(
                    "precondition_failed", details={"state": row.status.value}
                ) from exc
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
