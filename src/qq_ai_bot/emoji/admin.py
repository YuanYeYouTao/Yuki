"""Deterministic superuser administration for the emoji system."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage, MessageAttachment
from qq_ai_bot.emoji.collector import EmojiCollector
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.models import EmojiAsset, EmojiLifecycleStatus
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.emoji.worker import EmojiWorker
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.media_resolver import OneBotMediaGateway


class EmojiAdminService:
    """Share one business service between control-plane commands."""

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

    async def execute(
        self,
        *,
        actor: AdminActor,
        message: InboundMessage,
        argument: str,
        gateway: OneBotMediaGateway | None = None,
    ) -> str:
        try:
            return await self._execute(
                actor=actor,
                message=message,
                argument=argument,
                gateway=gateway,
            )
        except (LookupError, ValueError) as exc:
            return f"操作未完成：{exc}"

    async def status(self) -> dict[str, Any]:
        return {
            "counts": await self._repository.counts(),
            "worker_running": self._worker is not None and self._worker.running,
        }

    async def cleanup_expired(self) -> int:
        deleted, _temporary = await self._cleanup_counts()
        return deleted

    async def _execute(
        self,
        *,
        actor: AdminActor,
        message: InboundMessage,
        argument: str,
        gateway: OneBotMediaGateway | None = None,
    ) -> str:
        if not actor.is_superuser:
            return "权限不足：表情库管理仅限超级管理员。"
        parts = argument.strip().split()
        operation = parts[0].casefold() if parts else "list"
        arguments = parts[1:]
        if operation == "list":
            return await self._list(arguments)
        if operation == "show":
            asset = await self._target(arguments)
            return self._show(asset)
        if operation in {"adopt", "unadopt"}:
            return await self._adoption(operation, arguments, message)
        if operation in {"reject", "ban", "unban"}:
            return await self._status(operation, arguments)
        if operation in {"enable", "disable"}:
            return await self._asset_group_enabled(operation, arguments, message)
        if operation == "pin":
            return await self._pin(arguments)
        if operation == "reanalyze":
            asset = await self._target(arguments)
            await self._repository.enqueue(asset.id, "reanalyze")
            if self._worker is not None:
                self._worker.wake()
            return f"已将表情 {asset.id[:8]} 加入重新识别队列。"
        if operation == "group":
            return await self._group(actor, message, arguments)
        if operation == "stats":
            counts = await self._repository.counts()
            rendered = "，".join(f"{key}={value}" for key, value in sorted(counts.items()))
            return f"表情系统统计：{rendered or '暂无记录'}"
        if operation == "cleanup":
            return await self._cleanup()
        if operation == "doctor":
            return await self._doctor()
        if operation == "import":
            return await self._import(message, gateway)
        return self.help_text()

    @staticmethod
    def help_text() -> str:
        return (
            "/ai emoji list [candidate|recognized|adopted|rejected|banned|missing]\n"
            "/ai emoji show|adopt|unadopt|reject|ban|unban|reanalyze <ID>\n"
            "/ai emoji pin <ID> on|off\n"
            "/ai emoji enable|disable <ID> group current\n"
            "/ai emoji adopt|unadopt <ID> global|group [群号]\n"
            "/ai emoji group enable|disable（当前群）\n"
            "/ai emoji stats|cleanup|doctor\n"
            "/ai emoji import（和当前或回复图片一起发送）"
        )

    async def _list(self, arguments: list[str]) -> str:
        status = None
        if arguments:
            try:
                status = EmojiLifecycleStatus(arguments[0].casefold())
            except ValueError:
                return "状态无效。"
        rows = await self._repository.list_assets(status=status, limit=30)
        if not rows:
            return "表情库暂无匹配记录。"
        lines = [
            f"{row.id[:8]} [{row.status.value}] {row.description[:80] or '尚未识别'} "
            f"seen={row.seen_count} used={row.use_count}{' pinned' if row.pinned else ''}"
            for row in rows
        ]
        return f"表情记录（{len(rows)}）：\n" + "\n".join(lines)

    @staticmethod
    def _show(asset: EmojiAsset) -> str:
        return (
            f"ID：{asset.id}\n状态：{asset.status.value}\n格式：{asset.image_format} "
            f"{asset.width}x{asset.height}，{asset.frame_count} 帧\n"
            f"置信度：{asset.confidence:.2f}\n情绪：{', '.join(asset.emotion_tags) or '无'}\n"
            f"场景：{', '.join(asset.usage_scenarios) or '无'}\n"
            f"描述：{asset.description or '尚未识别'}"
        )

    async def _adoption(
        self,
        operation: str,
        arguments: list[str],
        message: InboundMessage,
    ) -> str:
        asset = await self._target(arguments)
        scope = arguments[1].casefold() if len(arguments) >= 2 else "global"
        if scope == "global":
            scope_type = "global"
            scope_id = ""
        elif scope == "group":
            scope_type = "group"
            scope_id = arguments[2] if len(arguments) >= 3 else (message.group_id or "")
            if not scope_id:
                return "请提供目标群号；私聊中没有“本群”。"
        else:
            return "作用域必须是 global 或 group。"
        if operation == "adopt":
            runtime = (await self._config.snapshot(group_id=scope_id or None)).emoji
            await self._lifecycle.adopt(
                asset.id,
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_id=scope_id,
                runtime=runtime,
            )
            return f"已在 {scope_type}:{scope_id or 'global'} 采用表情 {asset.id[:8]}。"
        removed = await self._lifecycle.unadopt(
            asset.id,
            scope_type=scope_type,  # type: ignore[arg-type]
            scope_id=scope_id,
        )
        return "已取消采用。" if removed else "该作用域原本没有采用这张表情。"

    async def _status(self, operation: str, arguments: list[str]) -> str:
        asset = await self._target(arguments)
        target = {
            "reject": EmojiLifecycleStatus.REJECTED,
            "ban": EmojiLifecycleStatus.BANNED,
            "unban": EmojiLifecycleStatus.RECOGNIZED,
        }[operation]
        updated = await self._lifecycle.transition(asset.id, target)
        return f"表情 {updated.id[:8]} 状态已改为 {updated.status.value}。"

    async def _pin(self, arguments: list[str]) -> str:
        if len(arguments) != 2 or arguments[1].casefold() not in {"on", "off"}:
            return "格式：/ai emoji pin <ID> on|off"
        asset = await self._target(arguments)
        updated = await self._repository.set_pinned(asset.id, arguments[1].casefold() == "on")
        return f"表情 {updated.id[:8]} pinned={'on' if updated.pinned else 'off'}。"

    async def _asset_group_enabled(
        self,
        operation: str,
        arguments: list[str],
        message: InboundMessage,
    ) -> str:
        asset = await self._target(arguments)
        if len(arguments) < 2 or arguments[1].casefold() != "group":
            return "格式：/ai emoji enable|disable <ID> group current"
        raw_group = arguments[2] if len(arguments) >= 3 else "current"
        group_id = message.group_id if raw_group.casefold() in {"current", "本群"} else raw_group
        if not group_id:
            return "私聊中没有当前群，请提供明确群号。"
        await self._repository.set_group_enabled(
            asset.id,
            group_id=group_id,
            enabled=operation == "enable",
        )
        return (
            f"已在群 {group_id} {'启用' if operation == 'enable' else '禁用'}表情 {asset.id[:8]}。"
        )

    async def _group(
        self,
        actor: AdminActor,
        message: InboundMessage,
        arguments: list[str],
    ) -> str:
        if message.group_id is None:
            return "该命令只能在群聊中使用。"
        if len(arguments) != 1 or arguments[0].casefold() not in {"enable", "disable"}:
            return "格式：/ai emoji group enable|disable"
        enabled = arguments[0].casefold() == "enable"
        result = await self._config.set(
            actor,
            key="emoji.enabled",
            value=enabled,
            scope_type="group",
            scope_id=message.group_id,
        )
        return (
            result.detail
            if not result.success
            else f"当前群表情系统已{'启用' if enabled else '停用'}。"
        )

    async def _import(
        self,
        message: InboundMessage,
        gateway: OneBotMediaGateway | None,
    ) -> str:
        attachments: tuple[MessageAttachment, ...] = (
            *message.attachments,
            *message.reply_attachments,
        )
        image = next((item for item in attachments if item.kind is AttachmentKind.IMAGE), None)
        if image is None:
            return "请把 /ai emoji import 和当前图片一起发送，或回复一张仍可读取的图片。"
        runtime = (
            await self._config.snapshot(
                user_id=message.sender.user_id,
                group_id=message.group_id,
            )
        ).emoji
        asset, created, restored = await self._collector.collect_attachment(
            image,
            message=message,
            source_event_id=None,
            runtime=runtime,
            gateway=gateway,
        )
        await self._repository.enqueue(asset.id, "reanalyze")
        if self._worker is not None:
            self._worker.wake()
        state = "新建" if created else ("恢复" if restored else "复用")
        return f"已{state}表情候选 {asset.id[:8]}，正在后台识别。"

    async def _cleanup(self) -> str:
        deleted, temporary = await self._cleanup_counts()
        return f"清理完成：删除过期候选 {deleted} 条、临时文件 {temporary} 个。"

    async def _cleanup_counts(self) -> tuple[int, int]:
        runtime = (await self._config.snapshot()).emoji
        before = datetime.now(UTC) - timedelta(days=runtime.cache_retention_days)
        candidates = await self._repository.cleanup_candidates(before=before)
        deleted = 0
        for asset in candidates:
            if not await self._repository.delete_asset(asset.id):
                continue
            self._storage.remove(asset.relative_path)
            self._storage.remove(asset.preview_relative_path)
            deleted += 1
        temporary = self._storage.cleanup_temporary_files()
        return deleted, temporary

    async def _doctor(self) -> str:
        rows = await self._repository.list_assets(limit=100000)
        missing = rebuilt = 0
        for asset in rows:
            if not self._storage.exists(asset.relative_path):
                if asset.status is not EmojiLifecycleStatus.MISSING:
                    await self._repository.set_status(asset.id, EmojiLifecycleStatus.MISSING)
                missing += 1
            elif asset.preview_relative_path and not self._storage.exists(
                asset.preview_relative_path
            ):
                await self._repository.enqueue(asset.id, "rebuild_preview")
                rebuilt += 1
        if self._worker is not None and rebuilt:
            self._worker.wake()
        return f"检查完成：原文件缺失 {missing} 条，待重建预览 {rebuilt} 条。"

    async def _target(self, arguments: list[str]) -> EmojiAsset:
        if not arguments:
            raise ValueError("请提供表情 ID")
        asset = await self._repository.resolve_id(arguments[0])
        if asset is None:
            raise ValueError("表情 ID 不存在或前缀不唯一")
        return asset

    @staticmethod
    def _asset_json(asset: EmojiAsset) -> dict[str, Any]:
        return {
            "emoji_id": asset.id,
            "status": asset.status.value,
            "description": asset.description,
            "emotion_tags": list(asset.emotion_tags),
            "usage_scenarios": list(asset.usage_scenarios),
            "confidence": asset.confidence,
            "animated": asset.animated,
            "pinned": asset.pinned,
            "seen_count": asset.seen_count,
            "use_count": asset.use_count,
        }
