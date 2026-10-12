"""Application service shared by Agent tools and deterministic commands."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.automation.authority import (
    DelegatedAuthority,
    PermissionLevel,
    permission_for_accounts,
)
from qq_ai_bot.automation.control_context import (
    ControlAutomationContext,
    CreationProvenance,
    resolve_control_context,
)
from qq_ai_bot.automation.creation_key import creation_key as _creation_key
from qq_ai_bot.automation.models import (
    AfterSchedule,
    AutomationDirectoryEntry,
    AutomationRecord,
    AutomationRunRecord,
    AutomationStatus,
    IntervalSchedule,
    OnceSchedule,
)
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.task_spec import TaskSpec, canonical_task_hash, prepare_task
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.persistence.unit_of_work import optional_session
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.time.schedules import initial_run_at
from qq_ai_bot.time.service import TimeContextService, validate_timezone

logger = logging.getLogger(__name__)


def _schedule_changed(before: AutomationRecord, after: TaskSpec) -> bool:
    if before.schedule.model_dump(exclude={"timezone"}) != after.trigger.model_dump(
        exclude={"timezone"}
    ):
        return True
    if isinstance(after.trigger, AfterSchedule | IntervalSchedule) or (
        isinstance(after.trigger, OnceSchedule) and after.trigger.local_datetime.tzinfo is not None
    ):
        return False
    return validate_timezone(
        getattr(before.schedule, "timezone", None) or before.timezone
    ) != validate_timezone(cast(str, getattr(after.trigger, "timezone", None) or after.timezone))


class AutomationService:
    """Create and manage only validated tasks owned by the real event sender."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: AutomationRepository,
        time_service: TimeContextService,
        audit: AdminAuditService | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._time = time_service
        self._audit = audit

    @property
    def enabled(self) -> bool:
        return self._settings.automation_enabled

    def management_schema(self) -> dict[str, object]:
        return {"task": TaskSpec.model_json_schema()}

    async def find_equivalent_directory_entries(
        self,
        task_payload: object,
        *,
        actor: ToolActor,
        max_runs: int | None = None,
    ) -> tuple[AutomationDirectoryEntry, ...]:
        _, _, provenance = await self._creator_context(actor)
        task = prepare_task(
            TaskSpec.model_validate(task_payload),
            timezone=await self._time.timezone_for(actor.user_id or "self"),
            current_group_id=provenance.current_group_id,
        )
        expected = task.model_dump(mode="json", exclude={"name"}, exclude_none=True)
        matched: list[AutomationDirectoryEntry] = []
        offset = 0
        while rows := await self._repository.list_directory(
            statuses=(AutomationStatus.ACTIVE, AutomationStatus.PAUSED),
            offset=offset,
            include_script=True,
        ):
            for entry in rows:
                record = cast(AutomationRecord, entry.record)
                actual = {key: value for key, value in record.script.items() if key != "name"}
                if record.max_runs == max_runs and actual == expected:
                    matched.append(entry)
            offset += len(rows)
        return tuple(matched)

    async def record_creation_failure(
        self,
        *,
        actor: ToolActor,
        conversation_key: str,
        error: Exception,
    ) -> None:
        """Persist a redacted failed creation attempt for later diagnosis."""

        if self._audit is None:
            return
        try:
            await self._audit.record(
                actor=self._audit_ref(actor, conversation_key),
                capability="automation",
                operation="create_task",
                target_type="automation_draft",
                target_id=actor.platform_message_id,
                after={"phase": "validate_or_commit"},
                success=False,
                error_category=type(error).__name__,
            )
        except Exception:
            logger.warning("automation_creation_failure_audit_unavailable", exc_info=True)

    async def diagnose_creation(self, creator_user_id: str) -> tuple[dict[str, object], ...]:
        """Return the caller's recent redacted creation outcomes."""

        if self._audit is None:
            return ()
        events = await self._audit.history(
            actor_user_id=creator_user_id,
            capability="automation",
            limit=20,
        )
        relevant = [event for event in events if event.operation in {"create", "create_task"}]
        return tuple(
            {
                "success": event.success,
                "error_category": event.error_category,
                "created_at": event.created_at.isoformat(),
                "target_id": event.target_id if event.success else None,
            }
            for event in relevant[:10]
        )

    async def _commit_task(
        self,
        task: TaskSpec,
        authority: DelegatedAuthority,
        *,
        creator_person_id: str,
        now: datetime,
        session: AsyncSession,
        automation_id: int | None = None,
        creation_source_key: str | None = None,
        max_runs: int | None = None,
        next_run_at: datetime | None = None,
    ) -> AutomationRecord:
        """Publish a complete task version with its real creation authority."""
        if automation_id is not None:
            row = await self._repository.publish_task(
                automation_id,
                creator_person_id=creator_person_id,
                task=task,
                authority=authority,
                now=now,
                next_run_at=next_run_at,
                session=session,
            )
            if row is None:
                raise ValueError("该任务已经结束，不能更新")
            return row
        if max_runs is not None and (type(max_runs) is not int or max_runs <= 0):
            raise ValueError("max_runs 必须是正整数")
        return await self._repository.create(
            task,
            authority,
            creator_person_id=creator_person_id,
            creation_source_key=creation_source_key,
            max_runs=max_runs,
            next_run_at=next_run_at
            if next_run_at is not None
            else initial_run_at(task.trigger, now, cast(str, task.timezone)),
            now=now,
            session=session,
        )

    async def create_task(
        self,
        task_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
        max_runs: int | None = None,
    ) -> AutomationRecord:
        if max_runs is not None and (
            isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs <= 0
        ):
            raise ValueError("max_runs 必须是正整数")
        started = time.perf_counter()
        try:
            task = TaskSpec.model_validate(task_payload)
        except ValidationError as exc:
            raise ValueError(f"定时任务格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        creator_person_id, permission, provenance = await self._creator_context(actor)
        task = prepare_task(
            task,
            timezone=await self._time.timezone_for(actor.user_id or "self"),
            current_group_id=provenance.current_group_id,
        )
        existing = await self._repository.get_by_creation_key(
            creator_person_id, _creation_key(actor.source_key)
        )
        if existing is not None:
            if (
                await self._repository.initial_script_hash(existing.id) != canonical_task_hash(task)
                or existing.max_runs != max_runs
            ):
                raise ValueError("automation_creation_key_conflict")
            return existing
        self._require_enabled()
        next_run_at = initial_run_at(task.trigger, now, cast(str, task.timezone))
        scene_fields: dict[str, object] = {}
        if actor.principal_kind == "self":
            scene_fields = await self._self_scene_fields(actor)
        elif actor.conversation_id is not None:
            from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

            async with self._repository._database.sessions() as session:
                conversation = await session.get(CanonicalConversationModel, actor.conversation_id)
            if conversation is not None:
                scene_fields = {
                    "canonical_creator_person_id": creator_person_id,
                    "canonical_conversation_id": conversation.id,
                    "conversation_generation": conversation.generation,
                    "canonical_presence_id": actor.presence_id,
                    "canonical_space_id": conversation.space_id,
                }
        authority = DelegatedAuthority(
            creator_user_id=actor.user_id,
            bot_user_id=actor.bot_user_id,
            created_from_message_id=actor.platform_message_id,
            created_at=now.isoformat(),
            permission_level=permission,
            current_group_id=actor.group_id,
            principal_kind=actor.principal_kind,
            **scene_fields,
        )
        async with self._repository._database.immediate_session() as session:
            current_owner, current_permission, _ = await self._creator_context(
                actor, session=session
            )
            if (current_owner, current_permission) != (creator_person_id, permission):
                raise PermissionError("automation_creation_authority_changed")
            if actor.principal_kind == "self":
                current_scene = await self._self_scene_fields(actor, session=session)
                if any(getattr(authority, key) != value for key, value in current_scene.items()):
                    raise PermissionError("self_automation_scene_changed")
            row = await self._commit_task(
                task,
                authority,
                creation_source_key=_creation_key(actor.source_key),
                creator_person_id=creator_person_id,
                max_runs=max_runs,
                now=now,
                next_run_at=next_run_at,
                session=session,
            )
            await self._audit_event(
                actor,
                conversation_key,
                operation="create",
                automation_id=row.id,
                after={"name": row.name, "status": row.status.value},
                started=started,
                session=session,
            )
        return row

    async def update_task(
        self,
        automation_id: int,
        task_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
    ) -> AutomationRecord:
        (
            existing,
            owner_person_id,
            owner_account_id,
            owner_permission,
            provenance,
        ) = await self._management_context(automation_id, actor)
        return await self._commit_update(
            existing,
            task_payload,
            actor=actor,
            conversation_key=conversation_key,
            owner_person_id=owner_person_id,
            owner_account_id=owner_account_id,
            owner_permission=owner_permission,
            provenance=provenance,
        )

    async def _commit_update(
        self,
        existing: AutomationRecord,
        task_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
        owner_person_id: str,
        owner_account_id: str,
        owner_permission: PermissionLevel,
        provenance: CreationProvenance,
    ) -> AutomationRecord:
        try:
            task = TaskSpec.model_validate(task_payload)
        except ValidationError as exc:
            raise ValueError(f"定时任务格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        task = prepare_task(
            task, timezone=existing.timezone, current_group_id=provenance.current_group_id
        )
        authority = DelegatedAuthority(
            creator_user_id=owner_account_id,
            bot_user_id=existing.bot_user_id,
            created_from_message_id="" if owner_person_id == "self" else actor.platform_message_id,
            created_at=now.isoformat(),
            permission_level=owner_permission,
            current_group_id=provenance.current_group_id,
            principal_kind=existing.creator_kind,
            **(
                {
                    key: existing.authority_snapshot.get(key)
                    for key in (
                        "canonical_conversation_id",
                        "conversation_generation",
                        "canonical_presence_id",
                        "canonical_space_id",
                    )
                }
            ),
        )
        async with self._repository._database.immediate_session() as session:
            current = await self.require_manageable(existing.id, actor, session=session)
            row = await self._commit_task(
                task,
                authority,
                automation_id=existing.id,
                creator_person_id=owner_person_id,
                now=now,
                next_run_at=initial_run_at(task.trigger, now, cast(str, task.timezone))
                if _schedule_changed(current, task)
                else None,
                session=session,
            )
            await self._audit_event(
                actor,
                conversation_key,
                operation="update",
                automation_id=row.id,
                before={"script_hash": current.script_hash},
                after={"script_hash": row.script_hash},
                session=session,
            )
        return row

    async def list(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return recent tasks across statuses; full directories use existing pagination."""

        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._repository.list_for_creator(creator_person_id)

    async def list_current(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return only active and paused tasks in current display order."""

        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._repository.list_current_for_creator(creator_person_id)

    async def list_for_actor(self, actor: ToolActor) -> tuple[AutomationRecord, ...]:
        owner, _, _ = await self._creator_context(actor)
        return await self._repository.list_current_for_creator(owner)

    async def list_directory(
        self,
        *,
        status: str = "active",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[AutomationDirectoryEntry, ...]:
        """Return the bounded Yuki-wide safe task directory, independent of owner."""

        statuses = {
            "active": (AutomationStatus.ACTIVE,),
            "paused": (AutomationStatus.PAUSED,),
            "current": (AutomationStatus.ACTIVE, AutomationStatus.PAUSED),
            "terminal": (
                AutomationStatus.COMPLETED,
                AutomationStatus.CANCELLED,
                AutomationStatus.FAILED,
                AutomationStatus.BLOCKED,
            ),
            "all": tuple(AutomationStatus),
        }.get(status)
        if statuses is None:
            raise ValueError("status 必须是 active、paused、current、terminal 或 all")
        return await self._repository.list_directory(
            statuses=statuses,
            limit=limit,
            offset=offset,
        )

    async def get_visible(self, automation_id: int) -> AutomationDirectoryEntry:
        """Return one task for safe read projection without granting mutation rights."""

        row = await self._repository.get_directory_entry(automation_id)
        if row is None:
            raise ValueError("自动化任务不存在")
        return row

    async def list_completed(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return terminal tasks in a separate newest-first history queue."""

        creator_person_id = (
            "self" if not creator_user_id else await self._resolve_creator_person(creator_user_id)
        )
        return await self._repository.list_terminal_for_creator(creator_person_id)

    async def require_manageable(
        self,
        automation_id: int,
        actor: ToolActor,
        *,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        """Allow the canonical owner or a current superuser to manage one task."""

        actor_person_id, permission, _provenance = await self._creator_context(
            actor, session=session
        )
        row = await self._repository.get(automation_id, session=session)
        if row is None:
            raise ValueError("自动化任务不存在")
        if (
            permission is not PermissionLevel.SUPERUSER
            and self._canonical_owner(row) != actor_person_id
        ):
            raise PermissionError("任务存在，但当前主体不是任务所有者，不能修改")
        return row

    async def pause(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        row = await self.require_manageable(automation_id, actor)
        creator_person_id = self._canonical_owner(row)
        async with self._repository._database.immediate_session() as session:
            await self.require_manageable(automation_id, actor, session=session)
            changed = await self._repository.set_status(
                automation_id,
                creator_person_id=creator_person_id,
                status=AutomationStatus.PAUSED,
                now=self._time.clock.now(),
                session=session,
            )
            await self._audit_event(
                actor,
                conversation_key,
                operation="pause",
                automation_id=automation_id,
                after={"changed": changed},
                session=session,
            )
        return changed

    async def resume(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        row = await self.require_manageable(automation_id, actor)
        creator_person_id = self._canonical_owner(row)
        now = self._time.clock.now()
        async with self._repository._database.immediate_session() as session:
            current = await self.require_manageable(automation_id, actor, session=session)
            TaskSpec.model_validate(current.script)
            next_run = current.next_run_at or now
            changed = await self._repository.resume(
                automation_id,
                creator_person_id=creator_person_id,
                next_run_at=next_run,
                now=now,
                session=session,
            )
            await self._audit_event(
                actor,
                conversation_key,
                operation="resume",
                automation_id=automation_id,
                after={"changed": changed},
                session=session,
            )
        return changed

    async def cancel(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        row = await self.require_manageable(automation_id, actor)
        creator_person_id = self._canonical_owner(row)
        async with self._repository._database.immediate_session() as session:
            await self.require_manageable(automation_id, actor, session=session)
            changed = await self._repository.cancel(
                automation_id,
                creator_person_id=creator_person_id,
                now=self._time.clock.now(),
                session=session,
            )
            await self._audit_event(
                actor,
                conversation_key,
                operation="cancel",
                automation_id=automation_id,
                after={"changed": changed},
                session=session,
            )
        return changed

    async def run_now(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        row = await self.require_manageable(automation_id, actor)
        creator_person_id = self._canonical_owner(row)
        async with self._repository._database.immediate_session() as session:
            current = await self.require_manageable(automation_id, actor, session=session)
            TaskSpec.model_validate(current.script)
            changed = await self._repository.schedule_now(
                automation_id,
                creator_person_id=creator_person_id,
                now=self._time.clock.now(),
                session=session,
            )
            await self._audit_event(
                actor,
                conversation_key,
                operation="run_now",
                automation_id=automation_id,
                after={"changed": changed},
                session=session,
            )
        return changed

    async def history(
        self, automation_id: int, *, actor: ToolActor, limit: int = 20
    ) -> tuple[AutomationRecord, tuple[AutomationRunRecord, ...]]:
        row = await self.require_manageable(automation_id, actor)
        return row, await self._repository.run_history(automation_id, limit=limit)

    async def current_time(self, user_id: str) -> dict[str, str]:
        return (await self._time.current(user_id)).to_model_dict()

    async def timezone(self, user_id: str) -> str:
        return await self._time.timezone_for(user_id)

    async def set_timezone(self, user_id: str, timezone: str) -> str:
        return await self._time.set_timezone(user_id, timezone)

    def _control_authority(self, context: ControlAutomationContext) -> DelegatedAuthority:
        return DelegatedAuthority(
            creator_user_id=context.provenance.creator_user_id,
            bot_user_id=context.provenance.bot_user_id,
            created_from_message_id="",
            created_at=self._time.clock.now().isoformat(),
            permission_level=context.provenance.permission,
            current_group_id=context.provenance.current_group_id,
            principal_kind="self" if context.owner_id == "self" else "person",
            canonical_conversation_id=context.conversation_id,
            conversation_generation=context.generation,
            canonical_presence_id=context.presence_id,
            canonical_space_id=context.space_id,
        )

    async def administer_create(
        self,
        task_payload: object,
        *,
        owner_id: str,
        creation_source_key: str,
        conversation_id: str | None = None,
        session: AsyncSession | None = None,
        max_runs: int | None = None,
    ) -> AutomationRecord:
        """Persist a control request with canonical ownership, never a fabricated event."""
        task = TaskSpec.model_validate(task_payload)
        now = self._time.clock.now()
        transaction = (
            self._repository._database.immediate_session()
            if session is None
            else optional_session(self._repository._database, session, write=True)
        )
        async with transaction as active:
            context = await resolve_control_context(
                active, self._settings, owner_id=owner_id, conversation_id=conversation_id
            )
            task = prepare_task(
                task,
                timezone=self._time.default_timezone,
                current_group_id=context.provenance.current_group_id,
            )
            existing = await self._repository.get_by_creation_key(
                owner_id, creation_source_key, session=active
            )
            if existing is not None:
                if (
                    await self._repository.initial_script_hash(existing.id, session=active)
                    != canonical_task_hash(task)
                    or existing.max_runs != max_runs
                ):
                    raise ValueError("automation_creation_key_conflict")
                return existing
            self._require_enabled()
            return await self._commit_task(
                task,
                self._control_authority(context),
                creator_person_id=owner_id,
                creation_source_key=creation_source_key,
                max_runs=max_runs,
                now=now,
                session=active,
            )

    async def administer_update(
        self,
        automation_id: int,
        task_payload: object,
        *,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        task = TaskSpec.model_validate(task_payload)
        now = self._time.clock.now()
        async with optional_session(self._repository._database, session, write=True) as active:
            existing = await self._repository.get(automation_id, session=active)
            if existing is None:
                raise LookupError("automation not found")
            owner_id = (
                "self" if existing.creator_kind == "self" else existing.canonical_creator_person_id
            )
            if owner_id is None:
                raise PermissionError("automation has no canonical creator")
            conversation_id = existing.authority_snapshot.get("canonical_conversation_id")
            if conversation_id is None and existing.canonical_target_space_id is not None:
                from sqlalchemy import select

                from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

                conversation_id = await active.scalar(
                    select(CanonicalConversationModel.id).where(
                        CanonicalConversationModel.space_id == existing.canonical_target_space_id
                    )
                )
            if conversation_id is not None and not isinstance(conversation_id, str):
                raise PermissionError("invalid automation scene")
            context = await resolve_control_context(
                active, self._settings, owner_id=owner_id, conversation_id=conversation_id
            )
            task = prepare_task(
                task,
                timezone=self._time.default_timezone,
                current_group_id=context.provenance.current_group_id,
            )
            row = await self._commit_task(
                task,
                self._control_authority(context),
                automation_id=automation_id,
                creator_person_id=owner_id,
                now=now,
                next_run_at=initial_run_at(task.trigger, now, cast(str, task.timezone))
                if _schedule_changed(existing, task)
                else None,
                session=active,
            )
            return row

    async def administer_transition(
        self,
        automation_id: int,
        *,
        action: str,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        existing = await self._repository.get(automation_id, session=session)
        if existing is None:
            raise LookupError("automation not found")
        owner_person_id = (
            "self" if existing.creator_kind == "self" else existing.canonical_creator_person_id
        )
        if owner_person_id is None:
            raise PermissionError("automation has no canonical creator")
        now = self._time.clock.now()
        if action == "pause":
            changed = await self._repository.set_status(
                automation_id,
                creator_person_id=owner_person_id,
                status=AutomationStatus.PAUSED,
                now=now,
                session=session,
            )
        elif action == "cancel":
            changed = await self._repository.cancel(
                automation_id,
                creator_person_id=owner_person_id,
                now=now,
                session=session,
            )
        elif action == "resume":
            TaskSpec.model_validate(existing.script)
            next_run = existing.next_run_at or now
            changed = await self._repository.resume(
                automation_id,
                creator_person_id=owner_person_id,
                next_run_at=next_run,
                now=now,
                session=session,
            )
        elif action == "run_now":
            TaskSpec.model_validate(existing.script)
            changed = await self._repository.schedule_now(
                automation_id,
                creator_person_id=owner_person_id,
                now=now,
                session=session,
            )
        else:
            raise ValueError(f"unsupported automation action: {action}")
        if not changed and action != "cancel":
            raise ValueError("automation state does not allow this action")
        current = await self._repository.get(automation_id, session=session)
        if current is None:
            raise LookupError("automation not found")
        return current

    async def _resolve_creator_person(
        self,
        external_account_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> str:
        person_id = await self._repository.resolve_active_creator_person(
            external_account_id,
            session=session,
        )
        if person_id is None:
            raise PermissionError("当前 QQ 账号没有活动的永久主体绑定")
        return person_id

    async def _creator_context(
        self,
        actor: ToolActor,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[str, PermissionLevel, CreationProvenance]:
        if actor.principal_kind == "self":
            if not actor.group_id or not actor.conversation_id or not actor.presence_id:
                raise PermissionError("self_automation_scene_required")
            permission = PermissionLevel.SELF
            return "self", permission, self._creation_provenance(actor, permission=permission)
        creator_person_id = await self._resolve_creator_person(actor.user_id, session=session)
        if actor.person_id is not None and actor.person_id != creator_person_id:
            raise PermissionError("actor_identity_changed")
        accounts = await self._repository.active_creator_accounts(
            creator_person_id, session=session
        )
        if not accounts:
            raise PermissionError("当前永久主体没有活动 QQ 绑定")
        permission = permission_for_accounts(self._settings, accounts)
        return (
            creator_person_id,
            permission,
            self._creation_provenance(actor, permission=permission),
        )

    async def _self_scene_fields(
        self, actor: ToolActor, *, session: AsyncSession | None = None
    ) -> dict[str, object]:
        from sqlalchemy import select

        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.identity.db_models import PresenceModel, SpaceBindingModel

        if not actor.conversation_id or not actor.presence_id or not actor.group_id:
            raise PermissionError("self_automation_scene_required")
        async with optional_session(self._repository._database, session, write=False) as session:
            if actor.origin is TurnOrigin.SCHEDULED_AUTOMATION:
                owner = await self._repository.get_for_run(actor.automation_run_id, session=session)
                if (
                    owner is None
                    or owner.creator_kind != "self"
                    or owner.canonical_presence_id != actor.presence_id
                ):
                    raise PermissionError("self_automation_source_changed")
            conversation = await session.get(CanonicalConversationModel, actor.conversation_id)
            presence = await session.get(PresenceModel, actor.presence_id)
            if conversation is None or not conversation.space_id:
                raise PermissionError("self_automation_scene_changed")
            if actor.origin is TurnOrigin.SCHEDULED_AUTOMATION and (
                owner is None or owner.canonical_target_space_id != conversation.space_id
            ):
                raise PermissionError("self_automation_source_changed")
            if actor.origin is TurnOrigin.SELF_INITIATIVE:
                from qq_ai_bot.conversation.self_initiative import validate_self_initiative

                if actor.initiative_run_id is None:
                    raise PermissionError("self_initiative_source_missing")
                await validate_self_initiative(
                    self._repository._database,
                    actor.initiative_run_id,
                    conversation_id=actor.conversation_id,
                    space_id=conversation.space_id,
                    presence_id=actor.presence_id,
                    session=session,
                )
            binding = await session.scalar(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.space_id == conversation.space_id,
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.status == "active",
                    SpaceBindingModel.external_space_id == actor.group_id,
                )
            )
            if (
                presence is None
                or not presence.enabled
                or presence.platform != "qq"
                or presence.external_account_id != actor.bot_user_id
                or binding is None
            ):
                raise PermissionError("self_automation_scene_changed")
            return {
                "canonical_conversation_id": conversation.id,
                "conversation_generation": conversation.generation,
                "canonical_presence_id": presence.id,
                "canonical_space_id": conversation.space_id,
            }

    async def _management_context(
        self,
        automation_id: int,
        actor: ToolActor,
    ) -> tuple[
        AutomationRecord,
        str,
        str,
        PermissionLevel,
        CreationProvenance,
    ]:
        """Authorize management while preserving the task owner's execution authority."""

        row = await self.require_manageable(automation_id, actor)
        owner_person_id = self._canonical_owner(row)
        from qq_ai_bot.automation.context import bind_automation_conversation

        async with self._repository._database.sessions() as session:
            conversation_id = row.authority_snapshot.get("canonical_conversation_id")
            if conversation_id is None:
                _, conversation_id = await bind_automation_conversation(session, row)
            context = await resolve_control_context(
                session, self._settings, owner_id=owner_person_id, conversation_id=conversation_id
            )
        owner_permission = context.provenance.permission
        provenance = context.provenance
        return (
            row,
            owner_person_id,
            context.provenance.creator_user_id,
            owner_permission,
            provenance,
        )

    @staticmethod
    def _canonical_owner(row: AutomationRecord) -> str:
        if row.creator_kind == "self":
            return "self"
        if row.canonical_creator_person_id is None:
            raise PermissionError("自动化任务没有永久创建者，不能修改")
        return row.canonical_creator_person_id

    def _require_enabled(self) -> None:
        if not self._settings.automation_enabled:
            raise ValueError("自动化功能当前未启用")

    def _creation_provenance(
        self,
        actor: ToolActor,
        *,
        permission: PermissionLevel,
    ) -> CreationProvenance:
        return CreationProvenance(
            creator_user_id=actor.user_id,
            bot_user_id=actor.bot_user_id,
            current_group_id=actor.group_id,
            permission=permission,
        )

    def _audit_ref(self, actor: ToolActor, conversation_key: str) -> ControlAuditRef:
        return ControlAuditRef(
            user_id=actor.user_id,
            principal_kind=actor.principal_kind,
            principal_id="self" if actor.principal_kind == "self" else actor.person_id,
            trigger_message_id=actor.platform_message_id,
            trigger_event_id=actor.event_id,
            decision_actor_id=actor.execution_id or None,
            canonical_conversation_id=actor.conversation_id,
            ingress_presence_id=actor.presence_id,
            conversation_key=conversation_key,
            bot_user_id=actor.bot_user_id,
        )

    async def _audit_event(
        self,
        actor: ToolActor,
        conversation_key: str,
        *,
        operation: str,
        automation_id: int,
        before: object = None,
        after: object = None,
        started: float | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            actor=self._audit_ref(actor, conversation_key),
            capability="automation",
            operation=operation,
            target_type="automation",
            target_id=str(automation_id),
            before=before,
            after=after,
            success=True,
            duration_seconds=(time.perf_counter() - started if started is not None else 0),
            session=session,
        )
