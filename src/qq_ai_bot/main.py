"""NoneBot2 application entrypoint and lifespan wiring."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

import nonebot
from nonebot.adapters.onebot.v11 import Bot
from nonebot.drivers.fastapi import Driver as FastAPIDriver

from qq_ai_bot.adapters.onebot.provider_adapter import (
    NapCatOneBotAdapter,
    SnowLumaOneBotAdapter,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.container import ApplicationContainer, get_container, set_container
from qq_ai_bot.gateway.registry import RegistryClosed
from qq_ai_bot.health import HealthPayload, build_health_payload
from qq_ai_bot.logging import configure_logging
from qq_ai_bot.persistence.instance_lock import SQLiteApplicationLock
from qq_ai_bot.persistence.schema_guard import require_canonical_schema


@contextmanager
def _nonebot_superusers_environment(superusers: frozenset[str]) -> Iterator[None]:
    """Expose SUPERUSERS in the JSON form expected by NoneBot during initialization."""

    previous = os.environ.get("SUPERUSERS")
    os.environ["SUPERUSERS"] = json.dumps(sorted(superusers))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SUPERUSERS", None)
        else:
            os.environ["SUPERUSERS"] = previous


def bootstrap(settings: Settings | None = None) -> None:
    """Configure NoneBot, routes, adapters, plugins, and resource lifecycle."""

    app_settings = settings or Settings()
    # Yuki accepts the long-standing comma-separated SUPERUSERS setting, while
    # NoneBot's environment source parses its own field as JSON before applying
    # the explicit value below. Temporarily normalize the shared variable so a
    # valid Yuki deployment cannot fail before the application starts.
    with _nonebot_superusers_environment(app_settings.superusers):
        nonebot.init(
            driver="~fastapi",
            host=app_settings.app_host,
            port=app_settings.app_port,
            log_level=app_settings.log_level,
            superusers=set(app_settings.superusers),
            onebot_access_token=app_settings.onebot_access_token or None,
        )
    configure_logging(app_settings.log_level)
    driver = nonebot.get_driver()
    driver.register_adapter(NapCatOneBotAdapter)
    driver.register_adapter(SnowLumaOneBotAdapter)
    application_lock = SQLiteApplicationLock(app_settings.sqlite_path)

    @driver.on_bot_connect
    async def _on_bot_connect(bot: Bot) -> None:
        from sqlalchemy import select

        from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
        from qq_ai_bot.identity.db_models import PresenceModel

        container = get_container()
        presence_id = None
        async with container.database.sessions() as session:
            presence = await session.scalar(
                select(PresenceModel).where(
                    PresenceModel.platform == IDENTITY_PLATFORM,
                    PresenceModel.external_account_id == str(bot.self_id),
                )
            )
            if presence is not None:
                presence_id = presence.id
        # Database resolution is asynchronous; the socket may already have
        # closed while it was pending. Bind only this still-live exact handle.
        try:
            container.gateway_registry.resolve_by_handle(bot)
        except RegistryClosed:
            return
        if presence_id is not None:
            container.gateway_registry.bind_presence(
                platform="qq",
                external_account_id=str(bot.self_id),
                presence_id=presence_id,
            )
        await container.route_monitor.on_connection_change()

    @driver.on_bot_disconnect
    async def _on_bot_disconnect(bot: Bot) -> None:
        container = get_container()
        await container.route_monitor.on_connection_change()

    @driver.on_startup
    async def startup() -> None:
        application_lock.acquire()
        try:
            await require_canonical_schema(app_settings.database_url)
            container = await ApplicationContainer.create(app_settings)
            set_container(container)
            await container.start()
        except BaseException:
            application_lock.release()
            raise

    @driver.on_shutdown
    async def shutdown() -> None:
        try:
            await get_container().close()
        finally:
            application_lock.release()

    async def healthz() -> HealthPayload:
        return await build_health_payload(get_container())

    if not isinstance(driver, FastAPIDriver):
        raise RuntimeError("FastAPI driver is required")
    driver.server_app.add_api_route("/healthz", healthz, methods=["GET"])

    nonebot.load_plugin("qq_ai_bot.plugins.ai_chat")


def run() -> None:
    """Run the ASGI server until SIGINT or SIGTERM."""

    bootstrap()
    nonebot.run()


if __name__ == "__main__":
    run()
