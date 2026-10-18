"""Commands for a person's memories, preferences, and identity."""

from __future__ import annotations

from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.control_resolution import ControlAccess, audit_ref_from_actor
from yuki.admin.models import AdminActor, ControlAuditRef
from yuki.domain.conversations import ScopeType
from yuki.domain.identity import PersonId
from yuki.domain.messages import InboundMessage
from yuki.domain.profiles import UserProfileSnapshot
from yuki.memory.rebuild.service import MemoryRebuildService
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import PeopleRepository
from yuki.services.admin.memory_admin import MemoryAdminService
from yuki.services.admin.preference_admin import PreferenceAdminService
from yuki.services.commands.memory import MemoryCommands


class ProfileCommandHandler:
    """Handle deterministic commands scoped to a canonical Person."""

    def __init__(
        self,
        *,
        people: PeopleRepository,
        memories: MemoryFactService,
        memory_admin: MemoryAdminService,
        preference_admin: PreferenceAdminService,
        control: ControlAccess,
        memory_rebuild: MemoryRebuildService | None = None,
        bot_display_name: str = "Yuki",
    ) -> None:
        self._people = people
        self._memories = memories
        self._memory_admin = memory_admin
        self._preference_admin = preference_admin
        self._control = control
        self._memory_rebuild = memory_rebuild
        self._bot_display_name = bot_display_name
        self._memory_commands = MemoryCommands(memory_admin, memory_rebuild)

    async def memory(self, *, actor: AdminActor, argument: str) -> str:
        return await self._memory_commands.memory(actor=actor, argument=argument)

    async def preference(self, *, actor: AdminActor, argument: str, admin: bool = False) -> str:
        parsed = self._parse_scoped_operation(
            argument,
            actor.person_id,
            admin,
        )
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        try:
            principal, audit = await self._authorized_call(actor)
            context = self._control.context(principal, await self._control.person_target(target))
            if operation == "list":
                rows = await self._preference_admin.list_preferences(context, audit)
                if not rows:
                    return f"Person {target} 暂无交互偏好。"
                return "\n".join(f"{row.key} = {row.value}" for row in rows)
            if operation == "set":
                if len(rest) < 2:
                    return "格式：/ai preference set <键> <值>"
                await self._preference_admin.set_preference(
                    context,
                    rest[0],
                    " ".join(rest[1:]),
                    audit=audit,
                )
                return f"偏好 {rest[0]} 已设置。"
            if operation == "delete":
                if len(rest) != 1:
                    return "格式：/ai preference delete <键>"
                deleted = await self._preference_admin.delete_preference(
                    context,
                    rest[0],
                    audit=audit,
                )
                return "偏好已删除。" if deleted else "没有找到该偏好。"
        except PermissionError as exc:
            return str(exc)
        return "可用操作：list、set、delete。"

    @staticmethod
    def _parse_scoped_operation(
        argument: str, actor: str | None, allow_target: bool
    ) -> tuple[str, str, list[str]] | str:
        if actor is None:
            return "没有找到当前 Person。"
        parts = argument.split()
        if not parts:
            return "缺少操作名。"
        operation = parts.pop(0).casefold()
        target = actor
        if len(parts) >= 2 and parts[0].casefold() == "user":
            if not allow_target:
                return "跨人物操作使用 /ai admin preference。"
            try:
                target = PersonId.parse(parts[1]).text
            except (TypeError, ValueError):
                return "目标 Person ID 格式错误。"
            del parts[:2]
        return operation, target, parts

    async def _authorized_call(self, actor: AdminActor) -> tuple[ControlPrincipal, ControlAuditRef]:
        return await self._control.principal_for_person(actor.person_id), audit_ref_from_actor(
            actor
        )

    async def whoami(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        argument: str,
    ) -> str:
        if argument:
            return "该命令不接受参数，只能查看发送者本人。"
        aliases = await self._people.aliases(person_id=message.person_id)
        memory_count = await self._memories.count_person(person_id=message.person_id)
        membership_count = await self._people.membership_count(person_id=message.person_id)
        lines = [
            f"Person：{message.person_id}",
            f"当前平台：{message.platform}；账号：{profile.user_id}",
            f"当前昵称：{profile.nickname or '未获取'}",
        ]
        if message.scope_type is ScopeType.GROUP:
            lines.extend(
                [
                    f"本群群名片：{profile.group_card or '未设置'}",
                    f"当前群：{profile.group_id}",
                ]
            )
        else:
            lines.append("当前场景：私聊")
        lines.extend(
            [
                f"已知别名：{'、'.join(aliases) if aliases else '无'}",
                f"个人记忆数：{memory_count}",
                f"群成员关系数：{membership_count}",
            ]
        )
        return "\n".join(lines)
