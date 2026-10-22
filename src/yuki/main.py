"""NoneBot2 application entrypoint and lifespan wiring."""

from __future__ import annotations

import nonebot
from fastapi import FastAPI
from nonebot.adapters.onebot.v11 import Bot
from nonebot.drivers.fastapi import Driver as FastAPIDriver

from yuki.adapters.onebot.provider_adapter import (
    SnowLumaOneBotAdapter,
)
from yuki.config import Settings
from yuki.container import ApplicationContainer, get_container, set_container
from yuki.gateway.registry import RegistryClosed
from yuki.health import HealthPayload, build_health_payload
from yuki.http_environment import load_http_environment
from yuki.logging import configure_logging
from yuki.persistence.instance_lock import SQLiteApplicationLock
from yuki.persistence.schema_guard import require_canonical_schema


def register_health_routes(app: FastAPI) -> None:
    """Keep process liveness separate from the database-backed diagnostic snapshot."""

    async def livez() -> dict[str, str]:
        # Docker only needs to know that the ASGI event loop can serve requests.
        # Dependency and worker diagnostics remain available at /healthz.
        return {"status": "alive"}

    async def healthz() -> HealthPayload:
        return await build_health_payload(get_container())

    app.add_api_route("/livez", livez, methods=["GET"])
    app.add_api_route("/healthz", healthz, methods=["GET"])


def bootstrap(settings: Settings | None = None) -> None:
    """Configure NoneBot, routes, adapters, plugins, and resource lifecycle."""

    load_http_environment()
    app_settings = settings or Settings()
    nonebot.init(
        driver="~fastapi",
        host=app_settings.app_host,
        port=app_settings.app_port,
        log_level=app_settings.log_level,
        onebot_access_token=app_settings.onebot_access_token or None,
    )
    configure_logging(app_settings.log_level)
    driver = nonebot.get_driver()
    if app_settings.onebot_access_token:
        driver.register_adapter(SnowLumaOneBotAdapter)
    application_lock = SQLiteApplicationLock(app_settings.sqlite_path)

    @driver.on_bot_connect
    async def _on_bot_connect(bot: Bot) -> None:
        from sqlalchemy import select

        from yuki.identity.canonical_repository import IDENTITY_PLATFORM
        from yuki.identity.db_models import PresenceModel

        container = get_container()
        presence_id = None
        async with container.database.read_session() as session:
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

    if not isinstance(driver, FastAPIDriver):
        raise RuntimeError("FastAPI driver is required")
    register_health_routes(driver.server_app)
    from yuki.admin_api.http import attach_admin_api

    attach_admin_api(driver.server_app, lambda: get_container().admin_api)
    from yuki.client_api.http import attach_client_api

    attach_client_api(driver.server_app, lambda: get_container().client_api)
    from yuki.external_agents.http import attach_external_agent_api

    attach_external_agent_api(driver.server_app, lambda: get_container().external_agents)
    if app_settings.onebot_access_token:
        nonebot.load_plugin("yuki.plugins.ai_chat")


def run() -> None:
    """Run the ASGI server until SIGINT or SIGTERM."""

    bootstrap()
    nonebot.run()


if __name__ == "__main__":
    run()
