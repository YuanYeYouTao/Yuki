"""Application service shared by Agent tools and deterministic commands."""

from __future__ import annotations

import logging
import time

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.automation.authority import (
    DelegatedAuthority,
    PermissionLevel,
    permission_for_accounts,
)
from qq_ai_bot.automation.compiler import AutomationCompiler, ExecutionPlan, TaskSpec
from qq_ai_bot.automation.creation_key import creation_key as _creation_key
from qq_ai_bot.automation.models import (
    AutomationDirectoryEntry,
    AutomationRecord,
    AutomationRunRecord,
    AutomationScript,
    AutomationStatus,
)
from qq_ai_bot.automation.registry import (
    AutomationCapabilityRegistry,
)
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.time.schedules import initial_run_at
from qq_ai_bot.time.service import TimeContextService

logger = logging.getLogger(__name__)


class AutomationService:
    """Create and manage only validated tasks owned by the real event sender."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: AutomationRepository,
        registry: AutomationCapabilityRegistry,
        time_service: TimeContextService,
        audit: AdminAuditService | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._registry = registry
        self._time = time_service
        self._validator = AutomationValidator(settings=settings, registry=registry)
        self._compiler = AutomationCompiler(settings=settings)
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
    ) -> tuple[AutomationRecord, ExecutionPlan]:
        """Compile, validate, and persist a high-level task as one atomic operation."""

        self._require_enabled()
        try:
            task = TaskSpec.model_validate(task_payload)
        except ValidationError as exc:
            raise ValueError(f"任务规格格式错误：{exc.errors()[0]['msg']}") from exc
        _creator_person_id, _permission, provenance = await self._creator_context(actor)
        plan = self._compiler.compile(
            task,
            provenance,
            default_timezone=await self._time.timezone_for(actor.user_id),
        )
        row = await self.create(
            plan.script,
            actor=actor,
            conversation_key=conversation_key,
            max_runs=max_runs,
        )
        return row, plan

    async def find_equivalent_task(
        self,
        task_payload: object,
        *,
        actor: ToolActor,
        max_runs: int | None = None,
    ) -> tuple[AutomationDirectoryEntry, ...]:
        """Exact structured candidates only; the Agent decides whether to create."""
        task = TaskSpec.model_validate(task_payload)
        _creator, _permission, provenance = await self._creator_context(actor)
        plan = self._compiler.compile(
            task,
            provenance,
            default_timezone=await self._time.timezone_for(actor.user_id),
        )
        expected = plan.script.model_dump(mode="json", exclude={"name"}, exclude_none=True)
        rows = await self._repository.list_directory(
            statuses=(AutomationStatus.ACTIVE, AutomationStatus.PAUSED),
            limit=200,
        )
        return tuple(
            entry
            for entry in rows
            if entry.record.max_runs == max_runs
            and entry.record.script.model_dump(mode="json", exclude={"name"}, exclude_none=True)
            == expected
        )

    async def update_task(
        self,
        automation_id: int,
        task_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
    ) -> tuple[AutomationRecord, ExecutionPlan]:
        """Compile and validate a high-level replacement before switching versions."""

        try:
            task = TaskSpec.model_validate(task_payload)
        except ValidationError as exc:
            raise ValueError(f"任务规格格式错误：{exc.errors()[0]['msg']}") from exc
        plan = self._compiler.compile(
            task,
            (await self._creator_context(actor))[2],
            default_timezone=await self._time.timezone_for(actor.user_id),
        )
        row = await self.update(
            automation_id,
            plan.script,
            actor=actor,
            conversation_key=conversation_key,
        )
        return row, plan

    async def record_creation_failure(
        self,
        *,
        actor: ToolActor,
        conversation_key: str,
        error: Exception,
    ) -> None:
        """Persist a redacted failed compile/create attempt for later diagnosis."""

        if self._audit is None:
            return
        try:
            await self._audit.record(
                actor=self._audit_ref(actor, conversation_key),
                capability="automation",
                operation="create_task",
                target_type="automation_draft",
                target_id=actor.platform_message_id,
                after={"phase": "compile_or_commit"},
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

    async def create(
        self,
        script_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
        max_runs: int | None = None,
    ) -> AutomationRecord:
        self._require_enabled()
        if max_runs is not None and (
            isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs <= 0
        ):
            raise ValueError("max_runs 必须是正整数")
        started = time.perf_counter()
        try:
            script = AutomationScript.model_validate(script_payload)
        except ValidationError as exc:
            raise ValueError(f"自动化脚本格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        creator_person_id, permission, provenance = await self._creator_context(actor)
        validated = self._validator.validate(script, provenance, now_utc=now)
        existing = await self._repository.get_by_creation_key(
            creator_person_id, _creation_key(actor.source_key)
        )
        if existing is not None:
            if existing.script_hash != validated.script_hash or existing.max_runs != max_runs:
                raise ValueError("automation_creation_key_conflict")
            return existing
        maximum = (
            self._settings.automation_max_active_per_superuser
            if permission.value == "superuser"
            else self._settings.automation_max_active_per_user
        )
        if await self._repository.active_count(creator_person_id) >= maximum:
            raise ValueError(f"当前用户最多同时启用 {maximum} 个自动化任务")
        authority = DelegatedAuthority(
            creator_user_id=actor.user_id,
            bot_user_id=actor.bot_user_id,
            created_from_message_id=actor.platform_message_id,
            created_at=now.isoformat(),
            permission_level=permission,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={
                name: self._registry.require(name).schema_version
                for name in validated.required_capabilities
            },
            capability_provenance=self._capability_provenance(validated.required_capabilities),
            current_group_id=actor.group_id,
        )
        row = await self._repository.create(
            validated,
            authority,
            creation_source_key=_creation_key(actor.source_key),
            creator_person_id=creator_person_id,
            max_runs=max_runs,
            misfire_grace_seconds=self._settings.automation_default_misfire_grace_seconds,
            now=now,
        )
        await self._audit_event(
            actor,
            conversation_key,
            operation="create",
            automation_id=row.id,
            after={"name": row.name, "status": row.status.value},
            started=started,
        )
        return row

    async def update(
        self,
        automation_id: int,
        script_payload: object,
        *,
        actor: ToolActor,
        conversation_key: str,
    ) -> AutomationRecord:
        creator_person_id, permission, provenance = await self._creator_context(actor)
        existing = await self._require_owned_person(automation_id, creator_person_id)
        try:
            script = AutomationScript.model_validate(script_payload)
        except ValidationError as exc:
            raise ValueError(f"自动化脚本格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        validated = self._validator.validate(
            script,
            provenance,
            now_utc=now,
        )
        authority = DelegatedAuthority(
            creator_user_id=actor.user_id,
            bot_user_id=actor.bot_user_id,
            created_from_message_id=actor.platform_message_id,
            created_at=now.isoformat(),
            permission_level=permission,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={
                name: self._registry.require(name).schema_version
                for name in validated.required_capabilities
            },
            capability_provenance=self._capability_provenance(validated.required_capabilities),
            current_group_id=actor.group_id,
        )
        row = await self._repository.update_script(
            automation_id,
            creator_person_id=creator_person_id,
            validated=validated,
            authority=authority,
            now=now,
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
        )
        return row

    async def list(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return all tasks for internal callers that need every status."""

        self._require_enabled()
        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._repository.list_for_creator(creator_person_id)

    async def list_current(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return only active and paused tasks in current display order."""

        self._require_enabled()
        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._repository.list_current_for_creator(creator_person_id)

    async def list_directory(
        self,
        *,
        status: str = "active",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[AutomationDirectoryEntry, ...]:
        """Return the bounded Yuki-wide safe task directory, independent of owner."""

        self._require_enabled()
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

        self._require_enabled()
        row = await self._repository.get_directory_entry(automation_id)
        if row is None:
            raise ValueError("自动化任务不存在")
        return row

    async def list_completed(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return terminal tasks in a separate newest-first history queue."""

        self._require_enabled()
        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._repository.list_terminal_for_creator(creator_person_id)

    async def require_owned(self, automation_id: int, creator_user_id: str) -> AutomationRecord:
        self._require_enabled()
        creator_person_id = await self._resolve_creator_person(creator_user_id)
        return await self._require_owned_person(automation_id, creator_person_id)

    async def pause(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        creator_person_id = await self._resolve_creator_person(actor.user_id)
        await self._require_owned_person(automation_id, creator_person_id)
        changed = await self._repository.set_status(
            automation_id,
            creator_person_id=creator_person_id,
            status=AutomationStatus.PAUSED,
            now=self._time.clock.now(),
        )
        await self._audit_event(
            actor,
            conversation_key,
            operation="pause",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def resume(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        creator_person_id = await self._resolve_creator_person(actor.user_id)
        row = await self._require_owned_person(automation_id, creator_person_id)
        now = self._time.clock.now()
        next_run = initial_run_at(row.script.schedule, now, row.timezone)
        changed = await self._repository.resume(
            automation_id,
            creator_person_id=creator_person_id,
            next_run_at=next_run,
            now=now,
        )
        await self._audit_event(
            actor,
            conversation_key,
            operation="resume",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def cancel(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        creator_person_id = await self._resolve_creator_person(actor.user_id)
        await self._require_owned_person(automation_id, creator_person_id)
        changed = await self._repository.set_status(
            automation_id,
            creator_person_id=creator_person_id,
            status=AutomationStatus.CANCELLED,
            now=self._time.clock.now(),
        )
        await self._audit_event(
            actor,
            conversation_key,
            operation="cancel",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def run_now(self, automation_id: int, *, actor: ToolActor, conversation_key: str) -> bool:
        creator_person_id = await self._resolve_creator_person(actor.user_id)
        await self._require_owned_person(automation_id, creator_person_id)
        changed = await self._repository.schedule_now(
            automation_id,
            creator_person_id=creator_person_id,
            now=self._time.clock.now(),
        )
        await self._audit_event(
            actor,
            conversation_key,
            operation="run_now",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def history(
        self, automation_id: int, *, creator_user_id: str, limit: int = 20
    ) -> tuple[AutomationRunRecord, ...]:
        await self.require_owned(automation_id, creator_user_id)
        return await self._repository.run_history(automation_id, limit=limit)

    async def current_time(self, user_id: str) -> dict[str, str]:
        return (await self._time.current(user_id)).to_model_dict()

    async def timezone(self, user_id: str) -> str:
        return await self._time.timezone_for(user_id)

    async def set_timezone(self, user_id: str, timezone: str) -> str:
        return await self._time.set_timezone(user_id, timezone)

    async def administer_create(
        self,
        script_payload: object,
        *,
        actor_user_id: str,
        trigger_message_id: str,
        session: AsyncSession | None = None,
        max_runs: int | None = None,
    ) -> AutomationRecord:
        """Create a validated task after control-plane capability authorization."""

        self._require_enabled()
        try:
            script = AutomationScript.model_validate(script_payload)
        except ValidationError as exc:
            raise ValueError(f"自动化脚本格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        actor_accounts = await self._repository.active_creator_accounts(
            actor_user_id,
            session=session,
        )
        if not actor_accounts:
            raise PermissionError("控制面主体没有活动 QQ 绑定")
        actor_account_id = actor_accounts[0]
        permission = permission_for_accounts(self._settings, actor_accounts)
        provenance = CreationProvenance(
            creator_user_id=actor_account_id,
            bot_user_id=actor_account_id,
            message_id=trigger_message_id,
            original_text="",
            current_group_id=None,
            mentioned_user_ids=(),
            permission=permission,
        )
        validated = self._validator.validate(script, provenance, now_utc=now)
        maximum = (
            self._settings.automation_max_active_per_superuser
            if permission is PermissionLevel.SUPERUSER
            else self._settings.automation_max_active_per_user
        )
        if await self._repository.active_count(actor_user_id) >= maximum:
            raise ValueError(f"当前用户最多同时启用 {maximum} 个自动化任务")
        authority = DelegatedAuthority(
            creator_user_id=actor_account_id,
            bot_user_id=actor_account_id,
            created_from_message_id=trigger_message_id,
            created_at=now.isoformat(),
            permission_level=permission,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={
                name: self._registry.require(name).schema_version
                for name in validated.required_capabilities
            },
            capability_provenance=self._capability_provenance(validated.required_capabilities),
            current_group_id=None,
        )
        return await self._repository.create(
            validated,
            authority,
            creator_person_id=actor_user_id,
            max_runs=max_runs,
            misfire_grace_seconds=self._settings.automation_default_misfire_grace_seconds,
            now=now,
            session=session,
        )

    async def administer_update(
        self,
        automation_id: int,
        script_payload: object,
        *,
        actor_user_id: str,
        trigger_message_id: str,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        self._require_enabled()
        existing = await self._repository.get(automation_id, session=session)
        if existing is None:
            raise LookupError("automation not found")
        owner_person_id = existing.canonical_creator_person_id
        if owner_person_id is None:
            raise PermissionError("automation has no canonical creator")
        actor_accounts = await self._repository.active_creator_accounts(
            actor_user_id,
            session=session,
        )
        if not actor_accounts:
            raise PermissionError("控制面主体没有活动 QQ 绑定")
        owner_accounts = await self._repository.active_creator_accounts(
            owner_person_id,
            session=session,
        )
        if not owner_accounts:
            raise PermissionError("自动化创建者没有活动 QQ 绑定")
        owner_account_id = owner_accounts[0]
        owner_permission = permission_for_accounts(self._settings, owner_accounts)
        try:
            script = AutomationScript.model_validate(script_payload)
        except ValidationError as exc:
            raise ValueError(f"自动化脚本格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        provenance = CreationProvenance(
            creator_user_id=owner_account_id,
            bot_user_id=existing.bot_user_id,
            message_id=trigger_message_id,
            original_text="",
            current_group_id=None,
            mentioned_user_ids=(),
            permission=owner_permission,
        )
        validated = self._validator.validate(script, provenance, now_utc=now)
        authority = DelegatedAuthority(
            creator_user_id=owner_account_id,
            bot_user_id=existing.bot_user_id,
            created_from_message_id=existing.created_from_message_id,
            created_at=now.isoformat(),
            permission_level=owner_permission,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={
                name: self._registry.require(name).schema_version
                for name in validated.required_capabilities
            },
            capability_provenance=self._capability_provenance(validated.required_capabilities),
            current_group_id=None,
        )
        row = await self._repository.update_script(
            automation_id,
            creator_person_id=owner_person_id,
            validated=validated,
            authority=authority,
            now=now,
            session=session,
        )
        if row is None:
            raise ValueError("该任务已经结束，不能更新")
        return row

    async def administer_transition(
        self,
        automation_id: int,
        *,
        action: str,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        self._require_enabled()
        existing = await self._repository.get(automation_id, session=session)
        if existing is None:
            raise LookupError("automation not found")
        owner_person_id = existing.canonical_creator_person_id
        if owner_person_id is None:
            raise PermissionError("automation has no canonical creator")
        now = self._time.clock.now()
        if action == "pause":
            await self._repository.set_status(
                automation_id,
                creator_person_id=owner_person_id,
                status=AutomationStatus.PAUSED,
                now=now,
                session=session,
            )
        elif action == "cancel":
            await self._repository.set_status(
                automation_id,
                creator_person_id=owner_person_id,
                status=AutomationStatus.CANCELLED,
                now=now,
                session=session,
            )
        elif action == "resume":
            next_run = initial_run_at(existing.script.schedule, now, existing.timezone)
            await self._repository.resume(
                automation_id,
                creator_person_id=owner_person_id,
                next_run_at=next_run,
                now=now,
                session=session,
            )
        elif action == "run_now":
            await self._repository.schedule_now(
                automation_id,
                creator_person_id=owner_person_id,
                now=now,
                session=session,
            )
        else:
            raise ValueError(f"unsupported automation action: {action}")
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
    ) -> tuple[str, PermissionLevel, CreationProvenance]:
        creator_person_id = await self._resolve_creator_person(actor.user_id)
        if actor.person_id is not None and actor.person_id != creator_person_id:
            raise PermissionError("actor_identity_changed")
        accounts = await self._repository.active_creator_accounts(creator_person_id)
        if not accounts:
            raise PermissionError("当前永久主体没有活动 QQ 绑定")
        permission = permission_for_accounts(self._settings, accounts)
        return (
            creator_person_id,
            permission,
            self._creation_provenance(actor, permission=permission),
        )

    async def _require_owned_person(
        self,
        automation_id: int,
        creator_person_id: str,
    ) -> AutomationRecord:
        row = await self._repository.get(automation_id)
        if row is None:
            raise ValueError("自动化任务不存在")
        if row.canonical_creator_person_id != creator_person_id:
            raise PermissionError("任务存在，但当前主体不是任务所有者，不能修改")
        return row

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
            message_id=actor.platform_message_id,
            original_text=actor.instruction,
            current_group_id=actor.group_id,
            mentioned_user_ids=actor.mentioned_user_ids,
            permission=permission,
        )

    def _audit_ref(self, actor: ToolActor, conversation_key: str) -> ControlAuditRef:
        return ControlAuditRef(
            user_id=actor.user_id,
            trigger_message_id=actor.platform_message_id,
            trigger_event_id=actor.event_id,
            decision_actor_id=actor.execution_id or None,
            canonical_conversation_id=actor.conversation_id,
            ingress_presence_id=actor.presence_id,
            conversation_key=conversation_key,
            bot_user_id=actor.bot_user_id,
        )

    def _capability_provenance(
        self,
        names: tuple[str, ...],
    ) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for name in names:
            definition = self._registry.require(name)
            if definition.provider_plugin_id is None:
                continue
            result[name] = {
                "plugin_id": definition.provider_plugin_id,
                "plugin_version": definition.provider_version or "",
                "manifest_hash": definition.provider_manifest_hash or "",
            }
        return result

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
        )
