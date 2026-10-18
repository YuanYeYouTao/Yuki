"""Current Conversation controls and read-only status presentation."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import load_only

from yuki import __version__
from yuki.admin.config_service import RuntimeConfigService
from yuki.automation.repository import AutomationRepository
from yuki.automation.worker import AutomationWorker
from yuki.config import Settings
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.conversation.projections import current_checkpoint_query
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.conversations import ConversationScope
from yuki.emoji.admin import EmojiAdminService
from yuki.persistence.repositories import PeopleRepository
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.vision_service import VisionService


class ConversationCommands:
    def __init__(
        self,
        *,
        settings: Settings,
        conversation_scopes: ConversationScopeRepository,
        people: PeopleRepository,
        concurrency: ConcurrencyManager,
        onebot_connected: Callable[[], bool],
        runtime_config: RuntimeConfigService,
        vision: VisionService | None,
        emoji: EmojiAdminService | None,
        automation_repository: AutomationRepository | None,
        automation_worker: AutomationWorker | None,
    ) -> None:
        self._settings = settings
        self._conversation_scopes = conversation_scopes
        self._people = people
        self._concurrency = concurrency
        self._onebot_connected = onebot_connected
        self._runtime_config = runtime_config
        self._vision = vision
        self._emoji_admin = emoji
        self._automation_repository = automation_repository
        self._automation_worker = automation_worker

    async def status(self, identity: ConversationScope, conversation_key: str) -> str:
        scope = await self._conversation_scopes.get(identity)
        async with self._people._database.sessions() as session:
            checkpoint = (
                await session.scalar(
                    current_checkpoint_query(scope.id).options(
                        load_only(
                            PromptProjectionModel.selected_summary_coverage,
                            PromptProjectionModel.updated_at,
                        )
                    )
                )
                if scope
                else None
            )
            compact_line = (
                f"Compact 覆盖点：{checkpoint.selected_summary_coverage}，"
                f"公共窗口更新时间：{checkpoint.updated_at.isoformat()}"
                if checkpoint is not None
                else "Compact 检查点：无"
            )
        pending_restart = await self._runtime_config.pending_restart_count()
        vision_busy = self._vision is not None and self._vision.busy
        vision_queue_depth = self._vision.queue_depth if self._vision is not None else 0
        vision_running = self._vision.running_count if self._vision is not None else 0
        emoji_status = await self._emoji_admin.status() if self._emoji_admin is not None else {}
        emoji_counts = emoji_status.get("counts", {})
        automation_count = (
            await self._automation_repository.active_count()
            if self._automation_repository is not None
            else 0
        )
        automation_last_run = (
            await self._automation_repository.latest_run_at()
            if self._automation_repository is not None
            else None
        )
        automation_next_run = (
            await self._automation_repository.next_due_at()
            if self._automation_repository is not None
            else None
        )
        automation_worker_status = (
            "运行中"
            if self._automation_worker is not None and self._automation_worker.running
            else "未运行"
        )
        automation_last_text = automation_last_run.isoformat() if automation_last_run else "无"
        automation_next_text = automation_next_run.isoformat() if automation_next_run else "无"
        text = (
            f"OneBot 连接：{'已连接' if self._onebot_connected() else '未连接'}\n"
            f"模型：{self._settings.llm_model or '未配置'}\n"
            f"视觉功能：{'已启用' if self._settings.vision_enabled else '未启用'}\n"
            f"视觉模型：{self._settings.vision_model or '未配置'}\n"
            f"视觉请求繁忙：{'是' if vision_busy else '否'}\n"
            f"视觉排队/运行：{vision_queue_depth}/{vision_running}\n"
            f"表情系统：{'已启用' if self._settings.emoji_enabled else '未启用'}\n"
            f"表情 Worker：{'运行中' if emoji_status.get('worker_running') else '未运行'}\n"
            f"表情候选/已采用/待处理："
            f"{emoji_counts.get('candidate', 0)}/"
            f"{emoji_counts.get('adopted', 0)}/"
            f"{emoji_counts.get('jobs_pending', 0)}\n" + compact_line + "\n"
            f"请求处理中："
            f"{'是' if self._concurrency.is_processing(conversation_key) else '否'}\n"
            f"待重启配置数：{pending_restart}\n"
            f"自动化：{'已启用' if self._settings.automation_enabled else '未启用'}\n"
            f"自动化 Worker：{automation_worker_status}\n"
            f"活跃自动化任务：{automation_count}\n"
            f"最近自动化执行：{automation_last_text}\n"
            f"最近待执行时间：{automation_next_text}\n"
            f"服务版本：{__version__}"
        )
        return text
