"""Model invocation statistics presented by chat."""

from __future__ import annotations

from yuki.config import Settings
from yuki.model_runtime.repository import ModelInvocationRepository


class ModelCommands:
    def __init__(self, settings: Settings, invocations: ModelInvocationRepository | None) -> None:
        self._settings = settings
        self._model_invocations = invocations

    async def stats(self) -> str:
        if self._model_invocations is None:
            return "模型调用统计尚未初始化。"
        stats, by_task, by_profile = await self._model_invocations.stats_summary()
        recent_errors = await self._model_invocations.recent_errors(
            limit=self._settings.model_stats_recent_error_limit
        )
        lines = [
            f"模型调用：{stats.invocations} 次（成功 {stats.successes}，失败 {stats.failures}）",
            f"Token：输入 {stats.prompt_tokens}，输出 {stats.completion_tokens}，"
            f"缓存命中 {stats.cached_prompt_tokens}，合计 {stats.total_tokens}；"
            f"缺少 usage {stats.unknown_usage} 次",
            f"平均延迟：{stats.average_latency_seconds:.3f} 秒",
            "按任务：",
        ]
        lines.extend(
            f"- {task}: {item.invocations} 次 / {item.total_tokens} Token"
            for task, item in sorted(by_task.items(), key=lambda pair: pair[0])
        )
        lines.append("按档案：")
        lines.extend(
            f"- {profile_id}: {item.invocations} 次 / {item.total_tokens} Token"
            for profile_id, item in sorted(by_profile.items())
        )
        lines.append("最近错误：")
        lines.extend(
            f"- {item.created_at.isoformat()} {item.task}/{item.profile_id}: {item.error_category}"
            for item in recent_errors
        )
        if not recent_errors:
            lines.append("- 无")
        text = "\n".join(lines)
        return text
