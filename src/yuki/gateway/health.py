"""Read current platform owners and connections for both health projections."""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import load_only

from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.db_models import AccountModel, PresenceModel
from yuki.persistence.database import Database

GatewayHealthSource = Callable[[], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class PlatformHealth:
    enabled: bool
    running: bool
    healthy: bool | None
    connected: bool
    owner: dict[str, object] | None = None

    def summary(self) -> dict[str, object]:
        return {"enabled": self.enabled, "running": self.running, "healthy": self.healthy}


async def platform_health(
    database: Database,
    providers: GatewayProviderCatalog,
    registry: GatewayConnectionRegistry,
    sources: Mapping[str, GatewayHealthSource],
) -> dict[str, PlatformHealth]:
    async with database.read_session() as session:
        presences = (
            await session.execute(
                select(PresenceModel.id, AccountModel, PresenceModel.enabled)
                .select_from(PresenceModel)
                .outerjoin(AccountModel, AccountModel.id == PresenceModel.account_id)
                .options(load_only(AccountModel.platform))
            )
        ).all()
    platforms = set(providers.platforms)
    enabled_by_platform: defaultdict[str, set[str]] = defaultdict(set)
    for presence_id, account, enabled in presences:
        platform = account.platform
        platforms.add(platform)
        if enabled:
            enabled_by_platform[platform].add(presence_id)
    platforms.update(sources.keys())
    result = {}
    for platform in sorted(platforms):
        enabled_ids = enabled_by_platform.get(platform, set())
        connected = bool(enabled_ids.intersection(registry.connected_presence_ids(platform)))
        owner = await sources[platform]() if platform in sources else None
        result[platform] = PlatformHealth(
            enabled=bool(owner["enabled"]) if owner is not None else bool(enabled_ids),
            running=bool(owner["running"]) if owner is not None else connected,
            healthy=(None if owner["healthy"] is None else bool(owner["healthy"]))
            if owner is not None
            else connected
            if enabled_ids
            else None,
            connected=registry.has_any_active(platform=platform),
            owner=owner,
        )
    return result
