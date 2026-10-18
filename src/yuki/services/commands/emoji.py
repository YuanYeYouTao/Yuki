"""Emoji command parsing and presentation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from yuki.admin.models import AdminActor
from yuki.domain.messages import InboundMessage
from yuki.emoji.admin import EmojiAdminService
from yuki.emoji.models import EmojiAsset, EmojiLifecycleStatus
from yuki.gateway.media import PlatformMediaGateway
from yuki.services.commands.help import ADMIN_ACTIONS


class EmojiCommands:
    def __init__(self, service: EmojiAdminService) -> None:
        self._service = service

    async def execute(
        self,
        *,
        actor: AdminActor,
        message: InboundMessage,
        argument: str,
        gateway: PlatformMediaGateway | None = None,
    ) -> str:
        await self._service.authorize(actor.person_id)
        parts = argument.strip().split()
        operation = parts[0].casefold() if parts else "list"
        arguments = parts[1:]
        if operation not in ADMIN_ACTIONS["emoji"]:
            return self.help_text()
        if operation == "list":
            return await self._list(arguments)
        if operation == "show":
            return self._show(await self._target(arguments))
        if operation == "stats":
            counts = (await self._service.status())["counts"]
            rendered = "，".join(f"{key}={value}" for key, value in sorted(counts.items()))
            return f"表情系统统计：{rendered or '暂无记录'}"
        if operation == "doctor":
            return await self._doctor()
        identity = ""
        scope_type: Literal["global", "group"] = "global"
        scope_id = ""
        enabled = True
        if operation in {
            "adopt",
            "unadopt",
            "reject",
            "ban",
            "unban",
            "pin",
            "enable",
            "disable",
            "reanalyze",
        }:
            identity = (await self._target(arguments)).id
        if operation in {"adopt", "unadopt"}:
            scope = arguments[1].casefold() if len(arguments) >= 2 else "global"
            if scope == "group":
                scope_type = "group"
                raw_scope = arguments[2] if len(arguments) >= 3 else "current"
                scope_id = (
                    message.space_id or "" if raw_scope.casefold() == "current" else raw_scope
                )
                if not scope_id:
                    return "请提供目标 Space ID；私聊中没有“本群”。"
            elif scope != "global":
                return "作用域必须是 global 或 group。"
        elif operation == "pin":
            if len(arguments) != 2 or arguments[1].casefold() not in {"on", "off"}:
                return "格式：/ai admin emoji pin <ID> on|off"
            enabled = arguments[1].casefold() == "on"
        elif operation in {"enable", "disable"}:
            if len(arguments) < 2 or arguments[1].casefold() != "group":
                return "格式：/ai admin emoji enable|disable <ID> group current"
            raw_scope = arguments[2] if len(arguments) >= 3 else "current"
            scope_id = message.space_id or "" if raw_scope.casefold() == "current" else raw_scope
            if not scope_id:
                return "私聊中没有当前群，请提供明确 Space ID。"
            scope_type = "group"
        elif operation == "group":
            if message.space_id is None:
                return "该命令只能在群聊中使用。"
            if len(arguments) != 1 or arguments[0].casefold() not in {"enable", "disable"}:
                return "格式：/ai admin emoji group enable|disable"
            scope_type, scope_id = "group", message.space_id
            enabled = arguments[0].casefold() == "enable"
        result = await self._service.mutate(
            actor,
            message,
            operation,
            identity=identity,
            scope_type=scope_type,
            scope_id=scope_id,
            enabled=enabled,
            gateway=gateway,
        )
        if not result.success:
            operation_id = result.operation.operation_id if result.operation is not None else ""
            return f"操作结果尚未确认，请查询 /ai admin operation {operation_id}。"
        change = result.effective_state["change"]
        assert isinstance(change, Mapping)
        if operation == "import":
            state = "新建" if change["created"] else ("恢复" if change["restored"] else "复用")
            return f"已{state}表情候选 {str(change['asset_id'])[:8]}，正在后台识别。"
        if operation == "cleanup":
            return (
                f"清理完成：删除过期候选 {change['deleted']} 条、"
                f"临时文件 {change['temporary']} 个。"
            )
        if operation == "repair":
            return (
                f"修复完成：标记原文件缺失 {change['missing']} 条，"
                f"已排队重建预览 {change['previews']} 条。"
            )
        if operation == "unadopt":
            return "已取消采用。" if change["removed"] else "该作用域原本没有采用这张表情。"
        if operation == "adopt":
            return f"已在 {scope_type}:{scope_id or 'global'} 采用表情 {identity[:8]}。"
        if operation in {"reject", "ban", "unban"}:
            return f"表情 {identity[:8]} 状态已改为 {change['status']}。"
        if operation == "pin":
            return f"表情 {identity[:8]} pinned={'on' if enabled else 'off'}。"
        if operation == "reanalyze":
            return f"已将表情 {identity[:8]} 加入重新识别队列。"
        if operation == "group":
            return f"当前群表情系统已{'启用' if enabled else '停用'}。"
        return (
            f"已在群 {scope_id} {'启用' if operation == 'enable' else '禁用'}表情 {identity[:8]}。"
        )

    @staticmethod
    def help_text() -> str:
        return (
            "/ai admin emoji list [candidate|recognized|adopted|rejected|banned|missing]\n"
            "/ai admin emoji show|adopt|unadopt|reject|ban|unban|reanalyze <ID>\n"
            "/ai admin emoji pin <ID> on|off\n"
            "/ai admin emoji enable|disable <ID> group current\n"
            "/ai admin emoji adopt|unadopt <ID> global|group [Space ID]\n"
            "/ai admin emoji group enable|disable（当前群）\n"
            "/ai admin emoji stats|cleanup\n"
            "/ai admin emoji doctor（查看原文件和预览缺失情况）\n"
            "/ai admin emoji repair（标记缺失原文件并排队重建预览）\n"
            "/ai admin emoji import（和当前或回复图片一起发送）"
        )

    async def _list(self, arguments: list[str]) -> str:
        status = None
        if arguments:
            try:
                status = EmojiLifecycleStatus(arguments[0].casefold())
            except ValueError:
                return "状态无效。"
        rows = await self._service.list_assets(status=status, limit=30)
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

    async def _doctor(self) -> str:
        missing, previews = await self._service.doctor()
        return (
            f"检查完成：原文件缺失 {missing} 条，预览缺失 {previews} 条。"
            " 使用 /ai admin emoji repair 标记缺失文件并重建预览。"
        )

    async def _target(self, arguments: list[str]) -> EmojiAsset:
        if not arguments:
            raise ValueError("请提供表情 ID")
        asset = await self._service.resolve_id(arguments[0])
        if asset is None:
            raise ValueError("表情 ID 不存在或前缀不唯一")
        return asset
