"""Platform credentials share the model bundle's permission, receipts and revision rules."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import select

from yuki.admin.authority.command_types import CommandOperation, ControlCommandError
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import Problem, ProblemCode
from yuki.admin.config_files import ConfigFileError
from yuki.admin.operations.base import CommandExecutor, authorize_current, fail, outcome
from yuki.admin.platform_files import PLATFORM_SCHEMAS, empty_platform
from yuki.client_api import snowluma_display
from yuki.gateway.health import PlatformHealth, platform_health
from yuki.gateway.platforms import PlatformGateways, PlatformPreflightError
from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.db_models import AccountModel, PresenceModel

_ABSENT = PlatformHealth(enabled=False, running=False, healthy=None, connected=False)


class PlatformOperations:
    def __init__(
        self,
        executor: CommandExecutor,
        gateways: PlatformGateways,
        providers: GatewayProviderCatalog,
        registry: GatewayConnectionRegistry,
    ) -> None:
        self.executor, self.database = executor, executor.database
        self.gateways, self.providers, self.registry = gateways, providers, registry

    async def read(self, principal: ControlPrincipal) -> dict[str, Any]:
        await authorize_current(self.database, principal, "control.config.file.content.read")
        try:
            bundle = await self.gateways.read()
        except ConfigFileError as exc:
            raise fail(ProblemCode(exc.category)) from None
        facts = await platform_health(
            self.database, self.providers, self.registry, self.gateways.health_sources
        )
        connected = {
            platform: self.registry.connected_presence_ids(platform)
            for platform in (*PLATFORM_SCHEMAS, "qq")
        }
        async with self.database.read_session() as session:
            accounts = (
                await session.execute(
                    select(PresenceModel.id, AccountModel)
                    .join(AccountModel, AccountModel.id == PresenceModel.account_id)
                    .where(
                        PresenceModel.id.in_([item for ids in connected.values() for item in ids])
                    )
                    .order_by(AccountModel.external_account_id)
                )
            ).all()

        def health(platform: str) -> dict[str, Any]:
            fact = facts.get(platform, _ABSENT)
            owner = fact.owner or {}
            return {
                "enabled": fact.enabled,
                "running": fact.running,
                "healthy": fact.healthy,
                "connected": fact.connected,
                "last_error_category": owner.get("last_error_category"),
                "last_success_at": owner.get("last_success_at"),
                "accounts": [
                    {
                        "presence_id": presence_id,
                        "external_account_id": account.external_account_id,
                        "display_name": account.display_name,
                    }
                    for presence_id, account in accounts
                    if account.platform == platform
                ],
            }

        entries: dict[str, Any] = {}
        for platform in PLATFORM_SCHEMAS:
            config = bundle.platforms.get(platform) or empty_platform(platform)
            entries[platform] = {
                "configured": config.configured,
                "enabled": config.enabled,
                "settings": dict(config.settings),
                "saved_secrets": sorted(config.secrets),
                "apply_mode": self.gateways.apply_mode(platform),
                "health": health(platform),
            }
        entries["qq"] = {
            "apply_mode": "restart",
            "savable": False,
            "login_available": snowluma_display.configured(),
            "health": health("qq"),
        }
        return {
            "revision": bundle.revision,
            "writable_directory": await asyncio.to_thread(
                os.access, self.gateways.path.absolute().parent, os.W_OK
            ),
            "platforms": entries,
        }

    async def apply(
        self, principal: ControlPrincipal, command: ControlCommand, spec: dict[str, Any]
    ) -> ControlResult:
        operation = CommandOperation.CONFIG_FILE_SAVE.value

        async def effect() -> Any:
            try:
                revision, applied = await self.gateways.apply(
                    command.expected_revision,
                    spec["platform"],
                    enabled=spec["enabled"],
                    settings=spec["settings"],
                    secrets=spec["secrets"],
                )
            except ConfigFileError as exc:
                raise fail(ProblemCode(exc.category)) from None
            except PlatformPreflightError as exc:
                raise ControlCommandError(
                    Problem(ProblemCode.PRECONDITION_FAILED, exc.details)
                ) from None
            return outcome(
                "platforms",
                revision,
                "applied" if applied else "saved_pending_restart",
                operation,
            )

        return await self.executor.external_command(
            principal,
            command,
            operation,
            "control.config.file.mutate",
            "platforms",
            "save",
            effect,
        )
