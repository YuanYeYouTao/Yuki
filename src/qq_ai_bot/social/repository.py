"""Durable claims for social effects; uncertainty is never a retry instruction."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialReceipt, SocialTarget

_ACTIONS = frozenset(
    {"send_private_message", "send_group_message", "poke_person", "recall_own_message"}
)


class SocialOperationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def prepare(
        self,
        *,
        source_turn_id: str,
        tool_call_id: str,
        source_conversation_id: str,
        action: str,
        target: SocialTarget,
        payload: dict[str, Any],
    ) -> SocialReceipt:
        if action not in _ACTIONS or not source_turn_id or not tool_call_id:
            raise SocialError("invalid_operation")
        if max(len(source_turn_id), len(tool_call_id)) > 128:
            raise SocialError("invalid_operation")
        encoded = json.dumps(
            {
                "action": action,
                "target": target.model_dump(mode="json"),
                "payload": payload,
                "source_conversation_id": source_conversation_id,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            target_row = await session.get(
                CanonicalPersonModel if target.kind == "person" else CanonicalSpaceModel,
                str(target.id),
            )
            if target_row is None:
                raise SocialError("target_not_found")
            await session.execute(
                insert(SocialOperationModel)
                .values(
                    id=str(uuid4()),
                    source_turn_id=source_turn_id,
                    tool_call_id=tool_call_id,
                    source_conversation_id=source_conversation_id,
                    action=action,
                    payload_hash=digest,
                    target_kind=target.kind,
                    target_id=str(target.id),
                    status=OperationStatus.PREPARED.value,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["source_turn_id", "tool_call_id"])
            )
            row = await session.scalar(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id == source_turn_id,
                    SocialOperationModel.tool_call_id == tool_call_id,
                )
            )
            assert row is not None
            if row.payload_hash != digest:
                raise SocialError("idempotency_conflict")
            return self._receipt(row)

    async def claim(self, operation_id: str, *, presence_id: str) -> bool:
        """Commit the send boundary before issuing any network action."""
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(SocialOperationModel)
                .where(
                    SocialOperationModel.id == operation_id,
                    SocialOperationModel.status == OperationStatus.PREPARED.value,
                )
                .values(
                    status=OperationStatus.EXECUTING.value,
                    presence_id=presence_id,
                    updated_at=datetime.now(UTC),
                )
            )
            return cast(CursorResult[Any], result).rowcount == 1

    async def finish(
        self,
        operation_id: str,
        *,
        status: OperationStatus,
        session: AsyncSession,
        platform_reference: str | None = None,
        error_category: str | None = None,
    ) -> None:
        """Caller commits a confirmed outgoing ledger append in this same transaction."""
        if status not in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.UNCERTAIN,
        }:
            raise SocialError("invalid_transition")
        if error_category is not None and (
            not error_category
            or len(error_category) > 64
            or not all(c.isascii() and (c.isalnum() or c == "_") for c in error_category)
        ):
            raise SocialError("invalid_error_category")
        if platform_reference is not None and len(platform_reference) > 255:
            raise SocialError("invalid_platform_reference")
        result = await session.execute(
            update(SocialOperationModel)
            .where(
                SocialOperationModel.id == operation_id,
                SocialOperationModel.status == OperationStatus.EXECUTING.value,
            )
            .values(
                status=status.value,
                platform_reference=platform_reference,
                error_category=error_category,
                updated_at=datetime.now(UTC),
            )
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise SocialError("invalid_transition")

    async def get(self, operation_id: str) -> SocialReceipt:
        async with self.database.sessions() as session:
            row = await session.get(SocialOperationModel, operation_id)
            if row is None:
                raise SocialError("operation_not_found")
            return self._receipt(row)

    async def recover_interrupted(self) -> int:
        """Startup-only recovery, before admitting turns; never execute old payloads."""
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(SocialOperationModel)
                .where(
                    SocialOperationModel.status == OperationStatus.EXECUTING.value,
                )
                .values(
                    status=OperationStatus.UNCERTAIN.value,
                    error_category="process_interrupted",
                    updated_at=datetime.now(UTC),
                )
            )
            return cast(CursorResult[Any], result).rowcount

    @staticmethod
    def _receipt(row: SocialOperationModel) -> SocialReceipt:
        return SocialReceipt(
            operation_id=row.id,
            action=row.action,
            status=OperationStatus(row.status),
            target=SocialTarget.model_validate({"kind": row.target_kind, "id": row.target_id}),
            presence_id=row.presence_id,
            platform_reference=row.platform_reference,
            error_category=row.error_category,
        )
