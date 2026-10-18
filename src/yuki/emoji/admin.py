"""Emoji administration operations over the original lifecycle and repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from yuki.admin.authority.commands import ControlResult
from yuki.admin.authority.json_types import JsonValue
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import AdminActor
from yuki.admin.operations.base import CommandExecutor, fail
from yuki.domain.messages import AttachmentKind, InboundMessage
from yuki.emoji.collector import EmojiCollector
from yuki.emoji.lifecycle import EmojiLifecycleService
from yuki.emoji.models import EmojiAsset, EmojiLifecycleStatus
from yuki.emoji.repository import EmojiRepository
from yuki.emoji.storage import EmojiStorage
from yuki.emoji.worker import EmojiWorker
from yuki.gateway.media import PlatformMediaGateway
from yuki.identity.authority import person_is_superuser
from yuki.persistence.command_receipts import CommandSuccess
from yuki.services.admin.config_admin import ConfigAdminService


class EmojiAdminService:
    def __init__(
        self,
        *,
        repository: EmojiRepository,
        lifecycle: EmojiLifecycleService,
        storage: EmojiStorage,
        collector: EmojiCollector,
        config: ConfigAdminService,
        worker: EmojiWorker | None,
    ) -> None:
        self._repository = repository
        self._lifecycle = lifecycle
        self._storage = storage
        self._collector = collector
        self._config = config
        self._worker = worker
        self._executor = CommandExecutor(repository._database)
        self._control = ControlAccess(repository._database)

    async def mutate(
        self,
        actor: AdminActor,
        message: InboundMessage,
        action: str,
        *,
        identity: str = "",
        scope_type: Literal["global", "group"] = "global",
        scope_id: str = "",
        enabled: bool = True,
        gateway: PlatformMediaGateway | None = None,
    ) -> ControlResult:
        """Keep one original intent around the emoji lifecycle's external work."""
        if message.source_event_id is None:
            raise ValueError("表情修改缺少原内部事件。")
        principal = await self._control.principal_for_person(actor.person_id)
        target = identity or f"event:{message.source_event_id}"
        payload: dict[str, JsonValue] = {
            "action": action,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "enabled": enabled,
        }

        async def receipt_revision() -> int:
            # The asset domain has no resource revision; this is the intent revision.
            return 1

        command = await self._executor.prepare_chat_command(
            principal,
            message.source_event_id,
            payload=payload,
            revision=receipt_revision,
        )

        async def effect() -> CommandSuccess:
            change: dict[str, JsonValue] = {"action": action}
            try:
                if action in {"adopt", "unadopt"}:
                    if action == "adopt":
                        runtime = (await self._config.snapshot(group_id=scope_id or None)).emoji
                        await self._lifecycle.adopt(
                            identity,
                            scope_type=scope_type,
                            scope_id=scope_id,
                            runtime=runtime,
                            actor_person_id=actor.person_id,
                        )
                    else:
                        change["removed"] = await self._lifecycle.unadopt(
                            identity,
                            scope_type=scope_type,
                            scope_id=scope_id,
                            actor_person_id=actor.person_id,
                        )
                elif action in {"reject", "ban", "unban"}:
                    status = {
                        "reject": EmojiLifecycleStatus.REJECTED,
                        "ban": EmojiLifecycleStatus.BANNED,
                        "unban": EmojiLifecycleStatus.RECOGNIZED,
                    }[action]
                    await self._lifecycle.transition(
                        identity, status, actor_person_id=actor.person_id
                    )
                    change["status"] = status.value
                elif action == "pin":
                    await self._repository.set_pinned(
                        identity, enabled, actor_person_id=actor.person_id
                    )
                elif action in {"enable", "disable"}:
                    await self._repository.set_group_enabled(
                        identity,
                        group_id=scope_id,
                        enabled=action == "enable",
                        actor_person_id=actor.person_id,
                    )
                elif action == "group":
                    result = await self._config.set(
                        actor,
                        key="emoji.enabled",
                        value=enabled,
                        scope_type="group",
                        scope_id=scope_id,
                    )
                    if not result.success:
                        raise fail(ProblemCode.VALIDATION_ERROR)
                    change["change_id"] = result.change_id
                elif action == "reanalyze":
                    await self._reanalyze(identity, actor_person_id=actor.person_id)
                elif action == "import":
                    asset, created, restored = await self._import_image(
                        message, gateway, actor_person_id=actor.person_id
                    )
                    change.update(asset_id=asset.id, created=created, restored=restored)
                elif action == "cleanup":
                    deleted, temporary = await self.cleanup_counts(actor_person_id=actor.person_id)
                    change.update(deleted=deleted, temporary=temporary)
                elif action == "repair":
                    missing, previews = await self.repair(actor_person_id=actor.person_id)
                    change.update(missing=missing, previews=previews)
                else:
                    raise fail(ProblemCode.VALIDATION_ERROR)
            except (ValueError, LookupError) as exc:
                raise fail(ProblemCode.VALIDATION_ERROR) from exc
            state: dict[str, JsonValue] = {
                "resource": target,
                "revision": 1,
                "status": "applied",
                "change": change,
            }
            return CommandSuccess(target, 1, "emoji", target, {}, state, state)

        return await self._executor.external_command(
            principal,
            command,
            "control.emoji.mutate",
            "control.emoji.mutate",
            target,
            action,
            effect,
        )

    async def status(self) -> dict[str, Any]:
        return {
            "counts": await self._repository.counts(),
            "worker_running": self._worker is not None and self._worker.running,
        }

    async def cleanup_expired(self) -> int:
        deleted, _temporary = await self.cleanup_counts()
        return deleted

    async def cleanup_counts(self, *, actor_person_id: str | None = None) -> tuple[int, int]:
        runtime = (await self._config.snapshot()).emoji
        before = datetime.now(UTC) - timedelta(days=runtime.cache_retention_days)
        candidates = await self._repository.cleanup_candidates(before=before)
        deleted = 0
        for asset in candidates:
            if not await self._repository.delete_asset(asset.id, actor_person_id=actor_person_id):
                continue
            self._storage.remove(asset.relative_path)
            self._storage.remove(asset.preview_relative_path)
            deleted += 1
        temporary = self._storage.cleanup_temporary_files()
        return deleted, temporary

    async def authorize(self, person_id: str | None) -> None:
        async with self._repository._database.sessions() as session:
            if not await person_is_superuser(session, person_id):
                raise PermissionError("权限不足：表情库管理仅限超级管理员。")

    async def list_assets(
        self, *, status: EmojiLifecycleStatus | None = None, limit: int = 30
    ) -> tuple[EmojiAsset, ...]:
        return await self._repository.list_assets(status=status, limit=limit)

    async def resolve_id(self, value: str) -> EmojiAsset | None:
        return await self._repository.resolve_id(value)

    async def _reanalyze(self, identity: str, *, actor_person_id: str | None = None) -> None:
        await self._repository.enqueue(identity, "reanalyze", actor_person_id=actor_person_id)
        if self._worker is not None:
            self._worker.wake()

    async def _import_image(
        self,
        message: InboundMessage,
        gateway: PlatformMediaGateway | None,
        *,
        actor_person_id: str | None = None,
    ) -> tuple[EmojiAsset, bool, bool]:
        image = next(
            (
                item
                for item in (*message.attachments, *message.reply_attachments)
                if item.kind is AttachmentKind.IMAGE
            ),
            None,
        )
        if image is None:
            raise ValueError("请将 /ai admin emoji import 与图片一起发送，或回复一张图片。")
        runtime = (
            await self._config.snapshot(user_id=message.person_id, group_id=message.space_id)
        ).emoji
        asset, created, restored = await self._collector.collect_attachment(
            image,
            message=message,
            source_event_id=message.source_event_id,
            runtime=runtime,
            gateway=gateway,
            actor_person_id=actor_person_id,
        )
        await self._reanalyze(asset.id, actor_person_id=actor_person_id)
        return asset, created, restored

    async def doctor(self) -> tuple[int, int]:
        rows = await self._repository.list_assets(limit=100000)
        missing = previews = 0
        for asset in rows:
            if not self._storage.exists(asset.relative_path):
                missing += 1
            elif asset.preview_relative_path and not self._storage.exists(
                asset.preview_relative_path
            ):
                previews += 1
        return missing, previews

    async def repair(self, *, actor_person_id: str | None = None) -> tuple[int, int]:
        rows = await self._repository.list_assets(limit=100000)
        missing = rebuilt = 0
        for asset in rows:
            if not self._storage.exists(asset.relative_path):
                if asset.status is not EmojiLifecycleStatus.MISSING:
                    await self._repository.set_status(
                        asset.id, EmojiLifecycleStatus.MISSING, actor_person_id=actor_person_id
                    )
                missing += 1
            elif asset.preview_relative_path and not self._storage.exists(
                asset.preview_relative_path
            ):
                rebuilt += int(
                    await self._repository.enqueue(
                        asset.id, "rebuild_preview", actor_person_id=actor_person_id
                    )
                )
        if self._worker is not None and rebuilt:
            self._worker.wake()
        return missing, rebuilt
