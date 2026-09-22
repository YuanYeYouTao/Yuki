"""GitHub Monitor-style lifecycle and administrator commands."""

from __future__ import annotations

import asyncio
import json
import shlex

from pydantic import BaseModel, Field, ValidationError

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import NotificationTarget, PermissionLevel, RestartPolicy, StrictModel
from yuki_plugin_sdk.registrar import (
    BackgroundServiceMetadata,
    BackgroundServiceRegistration,
    CommandMetadata,
    CommandRegistration,
    PluginRegistrar,
)
from yuki_plugin_sdk.results import CommandResult

from .config import MonitorConfig, Subscription, load_config, save_subscriptions
from .polling import DIAGNOSTICS_NAMESPACE, FeedPoller

USAGE = """monitor list | status | show <id>
monitor add <id> <feed-url> <group:群号|private:QQ号> [判断条件]
monitor set <完整订阅 JSON>
monitor pause <id> | resume <id> | remove <id>
set 支持 judge_template=any_update/release/reset_time、judge_prompt、include_any、exclude_any。
首次默认只建立基线；通知交给主 Agent，只有 send_message 回执才能证明已发送。"""


class CommandArguments(StrictModel):
    text: str = Field(default="", max_length=12000)


class SubscriptionMonitorPlugin:
    def __init__(self) -> None:
        self._context: PluginContext | None = None
        self._poller: FeedPoller | None = None
        self._stop = asyncio.Event()

    async def register(self, registrar: PluginRegistrar) -> None:
        registrar.register_config_schema(MonitorConfig)
        registrar.register_command(
            CommandRegistration(
                metadata=CommandMetadata(
                    name="monitor",
                    short_alias="monitor",
                    description="管理订阅源、通知判断条件、启停和处理状态。",
                    permission=PermissionLevel.SUPERUSER,
                    timeout_seconds=60,
                ),
                argument_model=CommandArguments,
                handler=self.command,
            )
        )
        registrar.register_background_service(
            BackgroundServiceRegistration(
                metadata=BackgroundServiceMetadata(
                    name="subscription_monitor",
                    description="Poll configured feeds and admit events to Yuki's Main Agent.",
                    restart_policy=RestartPolicy.ON_FAILURE,
                    max_concurrency=1,
                ),
                runner=self.run,
            )
        )

    async def start(self, context: PluginContext) -> None:
        context.features.require("notification.facade.v1")
        self._stop.clear()
        self._context = context
        self._poller = FeedPoller(context, self._stop)

    async def stop(self) -> None:
        self._stop.set()
        self._poller = None
        self._context = None

    async def run(self) -> None:
        if self._poller is None:
            raise RuntimeError("subscription monitor has not started")
        await self._poller.run()

    async def command(self, arguments: BaseModel) -> CommandResult:
        if self._context is None or self._poller is None:
            return CommandResult(ok=False, error_code="monitor.not_running", text="插件尚未启动。")
        context, poller = self._context, self._poller
        text = CommandArguments.model_validate(arguments.model_dump()).text.strip()
        action, _, rest = text.partition(" ")
        action = action.casefold() or "status"
        try:
            async with poller.config_lock:
                config = await load_config(context)
                if action in {"help", "?"}:
                    return CommandResult(text=USAGE)
                if action in {"list", "status"}:
                    lines = [f"Subscription Monitor：{len(config.subscriptions)} 条订阅"]
                    for subscription in config.subscriptions:
                        targets = ", ".join(
                            f"{t.target_type}:{t.target_id}" for t in subscription.targets
                        )
                        lines.append(
                            f"{subscription.id} [{'on' if subscription.enabled else 'off'}]"
                            f" → {targets}"
                        )
                        if action == "status":
                            _, state = await poller.load_state(subscription.id)
                            diagnostic = await context.storage.get(
                                DIAGNOSTICS_NAMESPACE, subscription.id
                            )
                            last_success = (
                                state.last_success_at.isoformat() if state.last_success_at else "-"
                            )
                            lines.append(
                                f"  基线={'已建立' if state.initialized else '待同步'}；"
                                f"待接纳={len(state.pending)}；已接纳={state.accepted}；规则过滤={state.filtered}；"
                                f"上次成功={last_success}"
                            )
                            if isinstance(diagnostic, dict):
                                lines.append(
                                    f"  错误={diagnostic.get('error_category', 'unknown')}"
                                )
                    if action == "status":
                        counts = await context.notifications.status()
                        counts_text = json.dumps(dict(counts), ensure_ascii=False)
                        lines.append(f"Host 通知/主 Agent 队列：{counts_text}")
                        lines.append(
                            "已接纳表示 Host 收到事件，不代表主 Agent 已决定发信或 QQ 已送达。"
                        )
                    return CommandResult(text="\n".join(lines)[:12000])
                if action == "set":
                    subscription = Subscription.model_validate_json(rest)
                    return await self._upsert(context, poller, config, subscription)
                parts = shlex.split(rest)
                if action == "show" and len(parts) == 1:
                    found = next((row for row in config.subscriptions if row.id == parts[0]), None)
                    if found is None:
                        return CommandResult(
                            ok=False, error_code="monitor.not_found", text="未找到该订阅。"
                        )
                    return CommandResult(text=found.model_dump_json(indent=2))
                if action == "add" and len(parts) >= 3:
                    kind, separator, target_id = parts[2].partition(":")
                    if not separator:
                        raise ValueError("target_missing")
                    subscription = Subscription(
                        id=parts[0],
                        url=parts[1],
                        targets=(
                            NotificationTarget.model_validate(
                                {"target_type": kind, "target_id": target_id}
                            ),
                        ),
                        judge_prompt=" ".join(parts[3:]),
                    )
                    if any(row.id == subscription.id for row in config.subscriptions):
                        return CommandResult(
                            ok=False,
                            error_code="monitor.exists",
                            text="ID 已存在；修改请使用 set。",
                        )
                    return await self._upsert(context, poller, config, subscription)
                if action in {"pause", "resume", "remove"} and len(parts) == 1:
                    existing = next(
                        (row for row in config.subscriptions if row.id == parts[0]), None
                    )
                    if existing is None:
                        return CommandResult(
                            ok=False, error_code="monitor.not_found", text="未找到该订阅。"
                        )
                    subscription = existing
                    async with poller.lock(subscription.id):
                        rows = tuple(
                            row.model_copy(update={"enabled": action == "resume"})
                            if row.id == subscription.id
                            else row
                            for row in config.subscriptions
                            if action != "remove" or row.id != subscription.id
                        )
                        await save_subscriptions(context, rows)
                        if action == "remove":
                            raw, state = await poller.load_state(subscription.id)
                            # Keep the cursor; discard only work not submitted to Host.
                            if state.pending:
                                await poller.save_state(
                                    subscription.id, raw, state.model_copy(update={"pending": ()})
                                )
                            await context.storage.delete(DIAGNOSTICS_NAMESPACE, subscription.id)
                    return CommandResult(
                        text=f"{subscription.id}：{action} 完成。Host 已接纳的轮次按原回执继续。"
                    )
        except (ValueError, ValidationError):
            return CommandResult(
                ok=False,
                error_code="monitor.invalid_arguments",
                text=f"参数无效，请核对 URL、目标和配置字段。\n{USAGE}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            context.logger.warning(
                "subscription_command_failed error_category=%s", type(exc).__name__
            )
            return CommandResult(
                ok=False,
                error_code="monitor.command_failed",
                text=f"操作失败：{type(exc).__name__}，请检查状态后再试。",
            )
        return CommandResult(ok=False, error_code="monitor.invalid_arguments", text=USAGE)

    async def _upsert(
        self,
        context: PluginContext,
        poller: FeedPoller,
        config: MonitorConfig,
        subscription: Subscription,
    ) -> CommandResult:
        async with poller.lock(subscription.id):
            _, state = await poller.load_state(subscription.id)
            previous = next(
                (row for row in config.subscriptions if row.id == subscription.id), None
            )
            if (state.url and state.url != subscription.url) or (
                previous and previous.url != subscription.url
            ):
                return CommandResult(
                    ok=False,
                    error_code="monitor.source_changed",
                    text="更换订阅源请使用新 ID，已有事件与游标仍属于原来源。",
                )
            rows = (
                *(row for row in config.subscriptions if row.id != subscription.id),
                subscription,
            )
            MonitorConfig(subscriptions=rows)
            for target in subscription.targets:
                await context.notifications.grant_target(target, bot_user_id="")
            await save_subscriptions(context, rows)
        return CommandResult(
            text=(
                f"已保存 {subscription.id}。首次同步：{subscription.initial_sync}；"
                "已有待处理事件保留原判断条件。"
            )
        )
