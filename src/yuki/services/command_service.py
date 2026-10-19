"""Finite chat routing to Conversation, self-service, and management adapters."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import AdminActor
from yuki.admin.permission_catalog import PermissionCatalogService
from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.worker import AutomationWorker
from yuki.config import Settings
from yuki.conversation.scope import runtime_conversation_key
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, OutboundMessage
from yuki.domain.profiles import UserProfileSnapshot
from yuki.gateway.media import PlatformMediaGateway
from yuki.memory.rebuild.service import MemoryRebuildService
from yuki.memory.service import MemoryFactService
from yuki.model_runtime.executor import ModelExecutor
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.persistence.repositories import PeopleRepository
from yuki.plugin_host.command_adapter import PluginCommandAdapter
from yuki.plugin_host.direct_command_router import DirectCommandMatch
from yuki.services.admin.config_admin import ConfigAdminService
from yuki.services.admin.memory_admin import MemoryAdminService
from yuki.services.admin.preference_admin import PreferenceAdminService
from yuki.services.automation_commands import AutomationCommandHandler
from yuki.services.commands.admin import AdminChatCommands
from yuki.services.commands.conversation import ConversationCommands
from yuki.services.commands.help import ADMIN_HELP, SELF_HELP
from yuki.services.commands.memory import MemoryCommands
from yuki.services.commands.models import ModelCommands
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.config_commands import ConfigCommandHandler
from yuki.services.policies import CommandName
from yuki.services.profile_commands import ProfileCommandHandler
from yuki.services.turn_coordinator import ConversationTurnCoordinator
from yuki.services.vision_service import VisionService

if TYPE_CHECKING:
    from yuki.admin.operations import AdminOperations
    from yuki.admin.queries import AdminQueries


@dataclass(frozen=True, slots=True)
class CommandExecution:
    text: str
    record_reply: bool = True
    outbound: OutboundMessage | None = None
    compact_focus: str | None = None


class CommandService:
    def __init__(
        self,
        *,
        settings: Settings,
        people: PeopleRepository,
        memories: MemoryFactService,
        concurrency: ConcurrencyManager,
        platform_connected: Callable[[ConversationScope], bool],
        model_executor: ModelExecutor,
        runtime_config: RuntimeConfigService,
        memory_admin: MemoryAdminService,
        preference_admin: PreferenceAdminService,
        config_admin: ConfigAdminService,
        permission_catalog: PermissionCatalogService,
        conversation_scopes: ConversationScopeRepository,
        admin_operations: AdminOperations,
        admin_queries: AdminQueries,
        memory_rebuild: MemoryRebuildService,
        vision_service: VisionService | None = None,
        automation_service: AutomationService | None = None,
        automation_repository: AutomationRepository | None = None,
        automation_worker: AutomationWorker | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        plugin_commands: PluginCommandAdapter | None = None,
        model_invocations: ModelInvocationRepository | None = None,
    ) -> None:
        self._people, self._runtime_config = people, runtime_config
        self._concurrency, self._turn_coordinator = concurrency, turn_coordinator
        self._plugin_commands = plugin_commands
        self._control = ControlAccess(people._database)
        memory_commands = MemoryCommands(memory_admin, memory_rebuild)
        self._profiles = ProfileCommandHandler(
            people=people,
            memories=memories,
            memory=memory_commands,
            preference_admin=preference_admin,
            control=self._control,
        )
        self._config = ConfigCommandHandler(
            config_admin=config_admin,
            permission_catalog=permission_catalog,
            operations=admin_operations,
        )
        self._automations = AutomationCommandHandler(
            settings=settings, automation_service=automation_service
        )
        self._conversation = ConversationCommands(
            settings=settings,
            conversation_scopes=conversation_scopes,
            people=people,
            concurrency=concurrency,
            platform_connected=platform_connected,
            model_executor=model_executor,
            runtime_config=runtime_config,
            vision=vision_service,
            automation_repository=automation_repository,
            automation_worker=automation_worker,
        )
        self._admin = AdminChatCommands(
            operations=admin_operations,
            queries=admin_queries,
            control=self._control,
            config=self._config,
            profiles=self._profiles,
            memory=memory_commands,
            models=ModelCommands(settings, model_invocations),
        )

    async def execute_direct_plugin(
        self, message: InboundMessage, identity: ConversationScope, match: DirectCommandMatch
    ) -> CommandExecution:
        if self._plugin_commands is None:
            return CommandExecution("插件直达命令当前不可用。")
        runtime = await self._runtime_config.snapshot(
            user_id=message.person_id, group_id=message.space_id
        )
        return CommandExecution(
            await self._plugin_commands.execute_direct(
                message=message, identity=identity, match=match, runtime=runtime
            )
        )

    async def execute(
        self,
        command: CommandName,
        message: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        argument: str,
        started: float,
        gateway: PlatformMediaGateway | None = None,
    ) -> CommandExecution:
        principal = await self._control.principal_for_person(message.person_id)
        is_superuser = "superuser" in principal.roles
        conversation_key = runtime_conversation_key(identity=identity, inbound=message)
        actor = AdminActor(
            user_id=message.sender.user_id,
            is_superuser=is_superuser,
            trigger_message_id=message.message_id,
            trigger_event_id=message.source_event_id,
            canonical_conversation_id=message.conversation_id,
            ingress_presence_id=message.presence_id,
            conversation_key=conversation_key,
            current_group_id=message.group_id,
            bot_user_id=message.bot_user_id,
            person_id=principal.principal_id.text,
            space_id=message.space_id,
            platform=message.platform,
        )
        if command is CommandName.HELP:
            text = SELF_HELP + ("\n" + ADMIN_HELP if is_superuser else "")
        elif command in {CommandName.NEW, CommandName.COMPACT}:
            if message.scope_type is ScopeType.GROUP and not is_superuser:
                text = "权限不足：群聊新会话和公共历史压缩仅限超级管理员。"
            elif command is CommandName.COMPACT:
                return CommandExecution("", record_reply=False, compact_focus=argument)
            else:
                text = "已为当前会话开始新的历史窗口；永久聊天账本和长期记忆仍然保留。"
        elif command is CommandName.STATUS:
            text = await self._conversation.status(identity, conversation_key)
        elif command is CommandName.STOP:
            if message.conversation_id:
                from yuki.runtime.work_repository import WorkRepository

                await WorkRepository(self._people._database).cancel(message.conversation_id)
            cancelled = await self._concurrency.cancel(conversation_key)
            if self._turn_coordinator is not None:
                cancelled = (
                    await self._turn_coordinator.cancel_interruptible(conversation_key) or cancelled
                )
            text = "已取消当前 AI 请求。" if cancelled else "当前没有正在处理的 AI 请求。"
        elif command is CommandName.PING:
            text = f"pong ({(time.perf_counter() - started) * 1000:.1f} ms)"
        elif command is CommandName.WHOAMI:
            text = await self._profiles.whoami(message, profile, argument)
        elif command is CommandName.FORGETME:
            if argument:
                text = "该命令不接受参数，只能删除发送者本人数据。"
            else:
                assert message.person_id is not None
                deleted = await self._people.delete_person_id(message.person_id)
                text = (
                    "已彻底删除与你当前人物关联的人物、记忆、成员关系和可归属聊天事件。"
                    if deleted
                    else "没有找到与你当前人物关联的数据。"
                )
        elif command is CommandName.MEMORY:
            text = await self._profiles.memory(actor=actor, argument=argument)
        elif command is CommandName.PREFERENCE:
            text = await self._profiles.preference(actor=actor, argument=argument)
        elif command is CommandName.CAPABILITIES:
            text = self._config.capabilities(message, argument, is_superuser=is_superuser)
        elif command is CommandName.AUTOMATION:
            text = await self._automations.execute(
                message=message, identity=identity, argument=argument
            )
        elif command is CommandName.PLUGIN:
            if self._plugin_commands is None:
                text = "插件系统当前未启用。"
            else:
                runtime = await self._runtime_config.snapshot(
                    user_id=message.person_id, group_id=message.space_id
                )
                text = await self._plugin_commands.execute(
                    message=message, identity=identity, argument=argument, runtime=runtime
                )
        elif command is CommandName.ADMIN:
            text = await self._admin.execute(principal, actor, message, argument, gateway)
        else:
            text = "未知命令，请使用 /ai help 查看帮助。"
        return CommandExecution(text=text, record_reply=command is not CommandName.FORGETME)
