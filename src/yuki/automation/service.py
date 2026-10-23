"""Application service shared by Agent tools and deterministic commands."""

from __future__ import annotations

import time
from enum import Enum

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.audit import AdminAuditService
from yuki.admin.models import ControlAuditRef
from yuki.automation.authority import (
    AutomationScene,
    PermissionLevel,
)
from yuki.automation.creation_key import creation_key as _creation_key
from yuki.automation.models import (
    AutomationDirectoryEntry,
    AutomationRecord,
    AutomationRunRecord,
    AutomationStatus,
)
from yuki.automation.repository import (
    AutomationRepository,
    PreparedTaskDefinition,
    _current_version_id,
    task_timezone,
)
from yuki.automation.task_spec import (
    TaskSpec,
    canonical_task_hash,
    legacy_creation_hash,
    prepare_task,
)
from yuki.config import Settings
from yuki.domain.tool_actor import ToolActor
from yuki.identity.person_identity import current_person_id, same_person_id
from yuki.persistence.models import AutomationModel
from yuki.persistence.unit_of_work import optional_session
from yuki.runtime.origin import TurnOrigin
from yuki.time.schedules import initial_run_at
from yuki.time.service import TimeContextService


class _Unset(Enum):
    VALUE = "unset"


UNSET = _Unset.VALUE


def _max_runs(value: int | None) -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("max_runs 必须是正整数")


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

    async def create_task(
        self,
        task_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
        max_runs: int | None = None,
        creation_source_key: str | None = None,
    ) -> AutomationRecord:
        _max_runs(max_runs)
        creator_person_id, permission = await self._creator_context(actor)
        creation_identity = _creation_key(creation_source_key or actor.source_key)
        existing = await self._repository.get_by_creation_key(creator_person_id, creation_identity)
        default_timezone = self._time.default_timezone
        # A confirmed Host invocation is normally answered from its original
        # receipt before dispatch. Exact original definitions remain readable
        # here too, without applying the new schema to an accepted old request.
        if existing is not None:
            saved = await self._repository.original_creation(existing.id)
            assert saved is not None
            original, original_scene, original_hash = saved
            default_timezone = task_timezone(original)
            if existing.creation_max_runs == max_runs and (
                original == task_payload
                or legacy_creation_hash(task_payload, original, original_scene) == original_hash
            ):
                return existing
            if "version" in original and "goal" in original and "trigger" in original:
                raise ValueError("automation_creation_key_conflict")
        try:
            task = prepare_task(TaskSpec.model_validate(task_payload), timezone=default_timezone)
        except ValidationError as exc:
            raise ValueError(f"定时任务格式错误：{exc.errors()[0]['msg']}") from exc
        if existing is not None:
            if original_hash != canonical_task_hash(task) or existing.creation_max_runs != max_runs:
                raise ValueError("automation_creation_key_conflict")
            return existing
        self._require_enabled()
        now = self._time.clock.now()
        next_run = initial_run_at(task.trigger, now)
        scene_fields: dict[str, object] = {}
        if actor.principal_kind == "self":
            scene_fields = await self._self_scene_fields(actor)
        elif actor.conversation_id is not None:
            from yuki.conversation.canonical_db_models import CanonicalConversationModel

            async with self._repository._database.read_session() as reader:
                conversation = await reader.get(CanonicalConversationModel, actor.conversation_id)
                if conversation is not None:
                    scene_fields = {
                        "canonical_conversation_id": conversation.id,
                        "conversation_generation": conversation.generation,
                        "canonical_presence_id": actor.presence_id,
                        "canonical_space_id": conversation.space_id,
                    }
        authority = AutomationScene(
            creator_user_id=actor.user_id,
            bot_user_id=actor.bot_user_id,
            created_from_message_id=actor.platform_message_id,
            current_group_id=actor.group_id,
            principal_kind=actor.principal_kind,
            canonical_creator_person_id=None if creator_person_id == "self" else creator_person_id,
            **scene_fields,
        )
        definition = PreparedTaskDefinition.prepare(task, authority)
        started = time.perf_counter()
        async with self._repository._database.immediate_session() as session:
            owner, current_permission = await self._creator_context(actor, session=session)
            if (owner, current_permission) != (creator_person_id, permission):
                raise PermissionError("automation_creation_authority_changed")
            if actor.principal_kind == "self":
                current_scene = await self._self_scene_fields(actor, session=session)
                if any(getattr(authority, key) != value for key, value in current_scene.items()):
                    raise PermissionError("self_automation_scene_changed")
            row = await self._repository.create(
                definition,
                creator_person_id=creator_person_id,
                creation_source_key=creation_identity,
                source_event_id=actor.event_id,
                max_runs=max_runs,
                now=now,
                next_run_at=next_run,
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
        max_runs: int | None | _Unset = UNSET,
    ) -> AutomationRecord:
        while True:
            existing = await self.require_manageable(automation_id, actor)
            if not isinstance(max_runs, _Unset):
                _max_runs(max_runs)
            try:
                task = TaskSpec.model_validate(task_payload)
            except ValidationError as exc:
                raise ValueError(f"定时任务格式错误：{exc.errors()[0]['msg']}") from exc
            if "name" not in task.model_fields_set:
                task = task.model_copy(update={"name": existing.script.get("name")})
            task = prepare_task(task, timezone=existing.timezone)
            authority = AutomationScene.model_validate(existing.authority_snapshot)
            definition = PreparedTaskDefinition.prepare(task, authority)
            now = self._time.clock.now()
            changed_schedule = existing.schedule != task.trigger
            next_run = initial_run_at(task.trigger, now) if changed_schedule else None
            async with self._repository._database.immediate_session() as session:
                current = await self._manageable_row(automation_id, actor, session)
                if await _current_version_id(session, automation_id) != existing.version_id:
                    continue
                row = await self._repository.publish_task(
                    automation_id,
                    creator_person_id=self._canonical_owner(current),
                    definition=definition,
                    now=now,
                    next_run_at=next_run,
                    max_runs=None if isinstance(max_runs, _Unset) else max_runs,
                    max_runs_set=not isinstance(max_runs, _Unset),
                    session=session,
                )
                if row is None:
                    raise ValueError("该任务已经结束，不能更新")
                await self._audit_event(
                    actor,
                    conversation_key,
                    operation="update",
                    automation_id=row.id,
                    before={"script_hash": existing.script_hash},
                    after={"script_hash": row.script_hash},
                    session=session,
                )
            return row

    async def list_current(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return only active and paused tasks in current display order."""

        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._repository.list_current_for_creator(creator_person_id)

    async def list_for_actor(self, actor: ToolActor) -> tuple[AutomationRecord, ...]:
        owner, _ = await self._creator_context(actor)
        return await self._repository.list_current_for_creator(owner)

    async def list_directory(
        self,
        *,
        status: str = "active",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[AutomationDirectoryEntry, ...]:
        """Return the bounded Yuki-wide safe task directory, independent of owner."""

        if status == "all":
            statuses = tuple(AutomationStatus)
        else:
            try:
                statuses = (AutomationStatus(status),)
            except ValueError:
                raise ValueError("status 必须是实际生命周期值或 all") from None
        return await self._repository.list_directory(
            statuses=statuses,
            limit=limit,
            offset=offset,
        )

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
    ) -> AutomationRecord:
        """Allow the canonical owner or a current superuser to manage one task."""
        async with self._repository._database.read_session() as session:
            await self._manageable_row(automation_id, actor, session)
            row = await self._repository.get(automation_id, session=session)
        assert row is not None
        return row

    async def _manageable_row(
        self,
        automation_id: int,
        actor: ToolActor,
        session: AsyncSession,
    ) -> AutomationModel:
        actor_person_id, permission = await self._creator_context(actor, session=session)
        row = await session.get(AutomationModel, automation_id)
        if row is None:
            raise ValueError("自动化任务不存在")
        if permission is not PermissionLevel.SUPERUSER:
            owner = self._canonical_owner(row)
            matches = owner == actor_person_id or (
                owner != "self"
                and actor_person_id != "self"
                and await same_person_id(session, owner, actor_person_id)
            )
            if not matches:
                raise PermissionError("任务存在，但当前主体不是任务所有者，不能修改")
        return row

    async def pause(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        async with self._repository._database.immediate_session() as session:
            current = await self._manageable_row(automation_id, actor, session)
            creator_person_id = self._canonical_owner(current)
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
        TaskSpec.model_validate((await self.require_manageable(automation_id, actor)).script)
        async with self._repository._database.immediate_session() as session:
            current = await self._manageable_row(automation_id, actor, session)
            creator_person_id = self._canonical_owner(current)
            now = self._time.clock.now()
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
        async with self._repository._database.immediate_session() as session:
            current = await self._manageable_row(automation_id, actor, session)
            creator_person_id = self._canonical_owner(current)
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
        TaskSpec.model_validate((await self.require_manageable(automation_id, actor)).script)
        async with self._repository._database.immediate_session() as session:
            current = await self._manageable_row(automation_id, actor, session)
            creator_person_id = self._canonical_owner(current)
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
        self, automation_id: int, *, actor: ToolActor, limit: int = 20, offset: int = 0
    ) -> tuple[AutomationRecord, tuple[AutomationRunRecord, ...]]:
        row = await self.require_manageable(automation_id, actor)
        from yuki.automation.run_delivery import read_run_deliveries

        runs = await self._repository.run_history(automation_id, limit=limit, offset=offset)
        return row, await read_run_deliveries(self._repository._database, runs)

    def current_time(self) -> dict[str, str]:
        return self._time.current_default().to_model_dict()

    async def _resolve_creator_person(
        self,
        person_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> str:
        from yuki.identity.db_models import CanonicalPersonModel

        async with optional_session(self._repository._database, session, write=False) as reader:
            person = await reader.get(CanonicalPersonModel, person_id)
            if person is None or not person.enabled:
                raise PermissionError("automation_owner_unavailable")
            root_id = await current_person_id(reader, person.id)
            root = await reader.get(CanonicalPersonModel, root_id)
            if root is None or not root.enabled:
                raise PermissionError("automation_owner_unavailable")
            return root_id

    async def _creator_context(
        self,
        actor: ToolActor,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[str, PermissionLevel]:
        if actor.principal_kind == "self":
            if not actor.group_id or not actor.conversation_id or not actor.presence_id:
                raise PermissionError("self_automation_scene_required")
            permission = PermissionLevel.SELF
            return "self", permission
        if actor.person_id is None:
            raise PermissionError("automation_owner_unavailable")
        await self._resolve_creator_person(actor.person_id, session=session)
        creator_person_id = actor.person_id
        from yuki.identity.authority import person_is_superuser

        async with optional_session(self._repository._database, session, write=False) as reader:
            permission = (
                PermissionLevel.SUPERUSER
                if await person_is_superuser(reader, creator_person_id)
                else PermissionLevel.USER
            )
        return creator_person_id, permission

    async def _self_scene_fields(
        self, actor: ToolActor, *, session: AsyncSession | None = None
    ) -> dict[str, object]:
        from sqlalchemy import select

        from yuki.conversation.canonical_db_models import CanonicalConversationModel
        from yuki.identity.db_models import PresenceModel, SpaceBindingModel

        if not actor.conversation_id or not actor.presence_id or not actor.group_id:
            raise PermissionError("self_automation_scene_required")
        async with optional_session(self._repository._database, session, write=False) as session:
            if actor.origin is TurnOrigin.SCHEDULED_AUTOMATION:
                owner = await self._repository.get_for_run(actor.automation_run_id, session=session)
                if (
                    owner is None
                    or owner.creator_kind != "self"
                    or owner.canonical_presence_id != actor.presence_id
                    or owner.canonical_conversation_id != actor.conversation_id
                ):
                    raise PermissionError("self_automation_source_changed")
            conversation = await session.get(CanonicalConversationModel, actor.conversation_id)
            presence = await session.get(PresenceModel, actor.presence_id)
            if conversation is None or not conversation.space_id:
                raise PermissionError("self_automation_scene_changed")
            if actor.origin is TurnOrigin.SCHEDULED_AUTOMATION and (
                owner is None
                or owner.authority_snapshot.get("canonical_space_id") != conversation.space_id
            ):
                raise PermissionError("self_automation_source_changed")
            if actor.origin is TurnOrigin.SELF_INITIATIVE:
                from yuki.conversation.self_initiative import validate_self_initiative

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
                    SpaceBindingModel.platform == actor.platform,
                    SpaceBindingModel.status == "active",
                    SpaceBindingModel.external_space_id == actor.group_id,
                )
            )
            if (
                presence is None
                or not presence.enabled
                or presence.platform != actor.platform
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

    @staticmethod
    def _canonical_owner(row: AutomationRecord | AutomationModel) -> str:
        if row.creator_kind == "self":
            return "self"
        if row.canonical_creator_person_id is None:
            raise PermissionError("自动化任务没有永久创建者，不能修改")
        return row.canonical_creator_person_id

    def _require_enabled(self) -> None:
        if not self._settings.automation_enabled:
            raise ValueError("自动化功能当前未启用")

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
