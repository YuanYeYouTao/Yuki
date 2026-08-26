"""GitHub Monitor plugin entrypoint."""

from __future__ import annotations

import asyncio

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import PermissionLevel, RestartPolicy
from yuki_plugin_sdk.registrar import (
    BackgroundServiceMetadata,
    BackgroundServiceRegistration,
    CommandMetadata,
    CommandRegistration,
    PluginRegistrar,
)

from .commands import GitHubCommandArguments, GitHubCommands
from .config import GitHubMonitorConfig
from .polling import GitHubPoller


class _ContextHolder:
    context: PluginContext | None = None
    poller: GitHubPoller | None = None


class GitHubMonitorPlugin:
    def __init__(self) -> None:
        self._holder = _ContextHolder()
        self._stop = asyncio.Event()
        self._commands = GitHubCommands(self._holder, self._stop)

    async def register(self, registrar: PluginRegistrar) -> None:
        registrar.register_config_schema(GitHubMonitorConfig)
        registrar.register_command(
            CommandRegistration(
                metadata=CommandMetadata(
                    name="github",
                    short_alias="github",
                    description="管理 GitHub 仓库监控、同步、测试和状态。",
                    permission=PermissionLevel.SUPERUSER,
                    timeout_seconds=30,
                ),
                argument_model=GitHubCommandArguments,
                handler=self._commands.handle,
            )
        )
        registrar.register_background_service(
            BackgroundServiceRegistration(
                metadata=BackgroundServiceMetadata(
                    name="github_monitor",
                    description="Poll configured GitHub repositories.",
                    restart_policy=RestartPolicy.ON_FAILURE,
                    max_concurrency=1,
                ),
                runner=self.run,
            )
        )

    async def start(self, context: PluginContext) -> None:
        context.features.require("notification.facade.v1")
        context.features.require("media.artifact.v1")
        context.features.require("http.credential.v1")
        self._stop.clear()
        self._holder.context = context
        self._holder.poller = GitHubPoller(context, self._stop)

    async def stop(self) -> None:
        self._stop.set()
        self._holder.poller = None
        self._holder.context = None

    async def run(self) -> None:
        poller = self._holder.poller
        if poller is None:
            raise RuntimeError("GitHub Monitor started without a PluginContext")
        await poller.run()
