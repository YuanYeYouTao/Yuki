"""Superuser management commands for GitHub Monitor."""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel, Field

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import NotificationTarget, StrictModel
from yuki_plugin_sdk.results import CommandResult

from .config import (
    GitHubMonitorConfig,
    NotificationTargetConfig,
    RepositorySubscription,
    load_config,
    save_repositories,
)
from .events import stable_event_key
from .models import NormalizedGitHubEvent
from .polling import GitHubPoller
from .state import load_queue_state, load_repository_state


class GitHubCommandArguments(StrictModel):
    text: str = Field(default="", max_length=2_000)


class GitHubCommands:
    def __init__(self, context_getter: object, stop: object) -> None:
        self._context_getter = context_getter
        self._stop = stop

    async def handle(self, raw: BaseModel) -> CommandResult:
        arguments = GitHubCommandArguments.model_validate(raw.model_dump())
        try:
            parts = shlex.split(arguments.text)
        except ValueError:
            return _error("github.invalid_arguments", "命令参数中的引号没有闭合。")
        action = parts[0].casefold() if parts else "status"
        context = self._context()
        config = await load_config(context)
        try:
            if action == "status":
                return await self._status(context, config)
            if action == "repos":
                return self._repos(config)
            if action == "add" and len(parts) == 3:
                return await self._add(context, config, parts[1], parts[2])
            if action == "remove" and len(parts) == 3:
                return await self._remove(context, config, parts[1], parts[2])
            if action in {"pause", "resume"} and len(parts) == 2:
                return await self._toggle(context, config, parts[1], action == "resume")
            if action == "sync" and len(parts) >= 2:
                return await self._sync(
                    context,
                    config,
                    parts[1],
                    parts[2] if len(parts) >= 3 else "baseline",
                )
            if action == "test" and len(parts) == 2:
                return await self._test(context, config, parts[1])
            if action == "events" and len(parts) == 2:
                return self._events(config, parts[1])
            if action == "rate-limit":
                return await self._rate_limit(context, config)
            if action == "outbox":
                return await self._outbox(context)
        except Exception as exc:
            context.logger.warning(
                "github_command_failed action=%s error_category=%s",
                action,
                type(exc).__name__,
            )
            return _error("github.command_failed", f"操作失败：{type(exc).__name__}")
        return _error("github.invalid_arguments", _usage())

    async def _status(
        self,
        context: PluginContext,
        config: GitHubMonitorConfig,
    ) -> CommandResult:
        counts = await context.notifications.status()
        lines = [f"GitHub Monitor：运行中，仓库 {len(config.repositories)} 个"]
        for subscription in config.repositories:
            state = (await load_queue_state(context, subscription.repository)).state
            lines.append(
                f"- {subscription.repository}：{'启用' if subscription.enabled else '暂停'}；"
                f"上次成功 {state.last_success_at.isoformat() if state.last_success_at else '无'}；"
                f"连续失败 {state.consecutive_failures}；Rate {state.rate_limit_remaining}；"
                f"cursor {state.committed_cursor or '-'}→{state.accepted_cursor or '-'}；"
                f"pending {len(state.pending)}；inflight {'有' if state.inflight else '无'}；"
                f"gap {state.gap_reason or '无'}"
            )
        lines.append(
            "Outbox pending={outbox_pending} failed={outbox_failed} uncertain={outbox_uncertain}；"
            "Turn pending={turn_pending} failed={turn_failed}".format(
                **{
                    key: counts.get(key, 0)
                    for key in (
                        "outbox_pending",
                        "outbox_failed",
                        "outbox_uncertain",
                        "turn_pending",
                        "turn_failed",
                    )
                }
            )
        )
        return CommandResult(text="\n".join(lines))

    @staticmethod
    def _repos(config: GitHubMonitorConfig) -> CommandResult:
        if not config.repositories:
            return CommandResult(text="尚未配置监控仓库。")
        lines = []
        for item in config.repositories:
            targets = ", ".join(f"{t.target_type}:{t.target_id}" for t in item.targets)
            lines.append(f"{item.repository} [{'on' if item.enabled else 'off'}] → {targets}")
        return CommandResult(text="\n".join(lines))

    async def _add(
        self,
        context: PluginContext,
        config: GitHubMonitorConfig,
        repository: str,
        raw_target: str,
    ) -> CommandResult:
        async with self._poller().configuration_guard(repository):
            config = await load_config(context)
            target = _parse_target(raw_target)
            candidate = RepositorySubscription(repository=repository, targets=(target,))
            rows = list(config.repositories)
            index = next(
                (
                    i
                    for i, item in enumerate(rows)
                    if item.repository.casefold() == repository.casefold()
                ),
                None,
            )
            if index is None:
                rows.append(candidate)
            else:
                existing = rows[index]
                if any(
                    item.target_type == target.target_type and item.target_id == target.target_id
                    for item in existing.targets
                ):
                    return CommandResult(text="该仓库目标已经存在。")
                rows[index] = existing.model_copy(update={"targets": (*existing.targets, target)})
            await context.notifications.grant_target(
                NotificationTarget(target_type=target.target_type, target_id=target.target_id),
                bot_user_id="",
            )
            await save_repositories(context, tuple(rows))
        return CommandResult(
            text=f"已添加 {candidate.repository} → {raw_target}；首次同步默认只建立基线。"
        )

    async def _remove(
        self,
        context: PluginContext,
        config: GitHubMonitorConfig,
        repository: str,
        raw_target: str,
    ) -> CommandResult:
        async with self._poller().configuration_guard(repository):
            config = await load_config(context)
            target = _parse_target(raw_target)
            rows: list[RepositorySubscription] = []
            removed = False
            for item in config.repositories:
                if item.repository.casefold() != repository.casefold():
                    rows.append(item)
                    continue
                targets = tuple(
                    value
                    for value in item.targets
                    if (value.target_type, value.target_id)
                    != (target.target_type, target.target_id)
                )
                removed = len(targets) != len(item.targets)
                if removed:
                    await self._poller().retire_target_locked(
                        item,
                        target,
                        last_target=not targets,
                        coalesce=config.coalesce,
                        max_batch_members=config.max_events_per_poll,
                    )
                if targets:
                    rows.append(item.model_copy(update={"targets": targets}))
            if not removed:
                return _error("github.not_found", "没有找到该仓库目标。")
            await save_repositories(context, tuple(rows))
            still_used = any(
                value.target_type == target.target_type and value.target_id == target.target_id
                for item in rows
                for value in item.targets
            )
            if not still_used:
                await context.notifications.revoke_target(
                    NotificationTarget(target_type=target.target_type, target_id=target.target_id)
                )
        return CommandResult(text=f"已移除 {repository} → {raw_target}。")

    async def _toggle(
        self,
        context: PluginContext,
        config: GitHubMonitorConfig,
        repository: str,
        enabled: bool,
    ) -> CommandResult:
        async with self._poller().configuration_guard(repository):
            config = await load_config(context)
            found = False
            rows = []
            for item in config.repositories:
                if item.repository.casefold() == repository.casefold():
                    item = item.model_copy(update={"enabled": enabled})
                    found = True
                rows.append(item)
            if not found:
                return _error("github.not_found", "没有找到该仓库。")
            await save_repositories(context, tuple(rows))
        return CommandResult(text=f"已{'恢复' if enabled else '暂停'} {repository}。")

    async def _sync(
        self,
        context: PluginContext,
        config: GitHubMonitorConfig,
        repository: str,
        mode: str,
    ) -> CommandResult:
        if mode not in {"baseline", "replay_recent"}:
            return _error("github.invalid_arguments", "同步模式只能是 baseline 或 replay_recent。")
        subscription = _find(config, repository)
        await self._poller().rebaseline(subscription, config, mode)
        return CommandResult(text=f"已按 {mode} 重新同步 {repository}。")

    async def _test(
        self,
        context: PluginContext,
        config: GitHubMonitorConfig,
        repository: str,
    ) -> CommandResult:
        subscription = _find(config, repository)
        now = datetime.now(UTC)
        event = NormalizedGitHubEvent(
            github_event_id=f"test-{int(now.timestamp())}",
            repository=subscription.repository,
            event_type="PushEvent",
            actor="yuki-test",
            created_at=now,
            branch="main",
            summary=f"{subscription.repository} 的 main 分支新增 2 个测试提交，修改 3 个文件",
            payload={
                "repository": subscription.repository,
                "actor": "yuki-test",
                "total_commits": 2,
                "files_changed": 3,
                "additions": 42,
                "deletions": 7,
                "commits": [
                    {"sha": "0123456", "message": "测试 GitHub 通知卡片"},
                    {"sha": "89abcde", "message": "验证主会话 Agent 点评"},
                ],
            },
            event_key=stable_event_key(
                subscription.repository,
                "test",
                str(int(now.timestamp() * 1000)),
            ),
        )
        await self._poller().publish_event(subscription, event)
        return CommandResult(text="测试事件已写入 Host；未修改真实 GitHub cursor。")

    @staticmethod
    def _events(config: GitHubMonitorConfig, repository: str) -> CommandResult:
        item = _find(config, repository)
        return CommandResult(text="已启用事件：" + ", ".join(sorted(item.event_types)))

    @staticmethod
    async def _rate_limit(
        context: PluginContext,
        config: GitHubMonitorConfig,
    ) -> CommandResult:
        lines = []
        for item in config.repositories:
            state = await load_repository_state(context, item.repository)
            lines.append(
                f"{item.repository}: remaining={state.rate_limit_remaining}, "
                f"reset={state.rate_limit_reset_at or '无'}, "
                f"request={state.last_request_id or '无'}"
            )
        return CommandResult(text="\n".join(lines) or "暂无 Rate Limit 数据。")

    @staticmethod
    async def _outbox(context: PluginContext) -> CommandResult:
        rows = await context.notifications.status()
        return CommandResult(
            text="\n".join(f"{key}: {value}" for key, value in sorted(rows.items()))
            or "Outbox 暂无记录。"
        )

    def _context(self) -> PluginContext:
        value = getattr(self._context_getter, "context", None)
        if value is None:
            raise RuntimeError("plugin is not running")
        return cast(PluginContext, value)

    def _poller(self) -> GitHubPoller:
        value = getattr(self._context_getter, "poller", None)
        if not isinstance(value, GitHubPoller):
            raise RuntimeError("github poller is not running")
        return value


def _parse_target(value: str) -> NotificationTargetConfig:
    target_type, separator, target_id = value.partition(":")
    if not separator or target_type not in {"group", "private"}:
        raise ValueError("target must use group:<id> or private:<id>")
    return NotificationTargetConfig(target_type=target_type, target_id=target_id)


def _find(config: GitHubMonitorConfig, repository: str) -> RepositorySubscription:
    for item in config.repositories:
        if item.repository.casefold() == repository.casefold():
            return item
    raise ValueError("repository is not configured")


def _error(code: str, detail: str) -> CommandResult:
    return CommandResult(ok=False, error_code=code, detail=detail, text=detail)


def _usage() -> str:
    return (
        "用法：/github status|repos|add owner/repo group:<id>|remove owner/repo group:<id>|"
        "pause owner/repo|resume owner/repo|sync owner/repo [baseline|replay_recent]|"
        "test owner/repo|events owner/repo|"
        "rate-limit|outbox"
    )
