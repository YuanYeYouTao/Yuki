"""Pure in-memory GatewayConnectionRegistry. Tokens and handles never persist."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from qq_ai_bot.gateway.models import (
    DEFAULT_NAPCAT_CAPABILITIES,
    ConnectionHealth,
    ConnectionSnapshot,
    PresenceConnectionSnapshot,
)


class RegistryClosed(RuntimeError):
    """Fail-closed selection: zero, many, or unpinned connections."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass
class _LiveConnection:
    connection_id: str
    gateway_instance_id: str
    provider: str
    platform: str
    external_account_id: str
    presence_id: str | None
    generation: int
    healthy: bool
    capabilities: frozenset[str]
    bot: object
    handle_id: int


@dataclass(frozen=True, slots=True)
class ConnectionResolution:
    snapshot: ConnectionSnapshot
    bot: object


def _account_key(platform: str, external_account_id: str) -> tuple[str, str]:
    return (platform.strip().casefold(), str(external_account_id).strip())


def _self_id(bot: object) -> str:
    return str(getattr(bot, "self_id", "")).strip()


class GatewayConnectionRegistry:
    """NapCat connect/disconnect bound to Presence. Selection is never first-item."""

    def __init__(
        self,
        *,
        gateway_instance_id: str | None = None,
        provider: str = "napcat",
    ) -> None:
        self.gateway_instance_id = (gateway_instance_id or str(uuid4())).strip()
        if not self.gateway_instance_id:
            raise ValueError("gateway_instance_id must be non-empty")
        self._provider = provider.strip().casefold() or "napcat"
        self._lock = threading.RLock()
        self._by_handle: dict[int, str] = {}
        self._by_id: dict[str, _LiveConnection] = {}
        self._by_account: dict[tuple[str, str], list[str]] = {}
        self._by_presence: dict[str, list[str]] = {}
        self._account_generation: dict[tuple[str, str], int] = {}
        self._account_presence: dict[tuple[str, str], str] = {}
        self._pins: dict[str, str] = {}

    def connect(
        self,
        bot: object,
        *,
        platform: str = "qq",
        presence_id: str | None = None,
        capabilities: frozenset[str] | None = None,
    ) -> ConnectionSnapshot:
        """Register a live handle. Reconnect of the same handle only increments generation."""

        if bot is None:
            raise TypeError("bot handle is required")
        external = _self_id(bot)
        if not external:
            raise ValueError("bot self_id is required")
        platform_key = platform.strip().casefold() or "qq"
        account = _account_key(platform_key, external)
        caps = capabilities if capabilities is not None else DEFAULT_NAPCAT_CAPABILITIES
        handle_id = id(bot)
        with self._lock:
            existing_id = self._by_handle.get(handle_id)
            if existing_id is not None:
                live = self._by_id[existing_id]
                live.generation += 1
                live.healthy = True
                live.bot = bot
                self._account_generation[account] = live.generation
                if presence_id:
                    self._bind_locked(account, presence_id)
                return self._snapshot(live)
            generation = self._account_generation.get(account, 0) + 1
            self._account_generation[account] = generation
            bound_presence = presence_id or self._account_presence.get(account)
            live = _LiveConnection(
                connection_id=str(uuid4()),
                gateway_instance_id=self.gateway_instance_id,
                provider=self._provider,
                platform=platform_key,
                external_account_id=external,
                presence_id=bound_presence,
                generation=generation,
                healthy=True,
                capabilities=frozenset(caps),
                bot=bot,
                handle_id=handle_id,
            )
            self._by_id[live.connection_id] = live
            self._by_handle[handle_id] = live.connection_id
            self._by_account.setdefault(account, []).append(live.connection_id)
            if bound_presence:
                self._index_presence(bound_presence, live.connection_id)
                self._account_presence[account] = bound_presence
                self._refresh_pin(bound_presence)
            return self._snapshot(live)

    def disconnect(self, bot: object) -> ConnectionSnapshot | None:
        """Drop the live handle. Generation is kept for the next reconnect."""

        if bot is None:
            return None
        handle_id = id(bot)
        with self._lock:
            connection_id = self._by_handle.pop(handle_id, None)
            if connection_id is None:
                return None
            live = self._by_id.pop(connection_id, None)
            if live is None:
                return None
            account = _account_key(live.platform, live.external_account_id)
            ids = self._by_account.get(account, [])
            self._by_account[account] = [item for item in ids if item != connection_id]
            if live.presence_id is not None:
                presence_ids = self._by_presence.get(live.presence_id, [])
                self._by_presence[live.presence_id] = [
                    item for item in presence_ids if item != connection_id
                ]
                if self._pins.get(live.presence_id) == connection_id:
                    self._pins.pop(live.presence_id, None)
                self._refresh_pin(live.presence_id)
            live.healthy = False
            live.bot = None
            return self._snapshot(live)

    def bind_presence(
        self,
        *,
        platform: str,
        external_account_id: str,
        presence_id: str,
    ) -> None:
        """Attach a later-created Presence to connections already keyed by account."""

        presence = presence_id.strip()
        if not presence:
            raise ValueError("presence_id is required")
        account = _account_key(platform, external_account_id)
        with self._lock:
            self._bind_locked(account, presence)

    def resolve_by_handle(self, bot: object) -> ConnectionResolution:
        """Reverse an event Bot handle to its exact connection. Never first-item."""

        if bot is None:
            raise RegistryClosed("no_connection")
        with self._lock:
            connection_id = self._by_handle.get(id(bot))
            if connection_id is None:
                raise RegistryClosed("no_connection")
            live = self._by_id.get(connection_id)
            if live is None or not live.healthy or live.bot is None:
                raise RegistryClosed("disconnected")
            return ConnectionResolution(snapshot=self._snapshot(live), bot=live.bot)

    def resolve_active(self, presence_id: str) -> ConnectionResolution:
        """Exactly one determined active connection for a Presence, or fail-closed."""

        presence = presence_id.strip()
        if not presence:
            raise RegistryClosed("no_connection")
        with self._lock:
            return self._resolve_ids(self._live_ids_for_presence(presence), pin_key=presence)

    def resolve_account(self, platform: str, external_account_id: str) -> ConnectionResolution:
        """Exactly one live connection for a platform account, or fail-closed."""

        account = _account_key(platform, external_account_id)
        with self._lock:
            presence = self._account_presence.get(account)
            return self._resolve_ids(self._live_ids_for_account(account), pin_key=presence)

    def snapshot_presence(
        self,
        *,
        presence_id: str,
        platform: str,
        external_account_id: str,
    ) -> PresenceConnectionSnapshot:
        """Control Query snapshot. Does not inspect NoneBot dictionaries."""

        account = _account_key(platform, external_account_id)
        with self._lock:
            ids = self._live_ids_for_presence(presence_id.strip())
            if not ids:
                ids = self._live_ids_for_account(account)
            live_count = len(ids)
            if live_count == 0:
                return PresenceConnectionSnapshot(
                    health=ConnectionHealth.DISCONNECTED,
                    generation=self._account_generation.get(account),
                    connection_id=None,
                    gateway_instance_id=self.gateway_instance_id,
                    live_count=0,
                )
            pin = self._pins.get(presence_id.strip())
            chosen: _LiveConnection | None = None
            if live_count == 1:
                chosen = self._by_id[ids[0]]
            elif pin is not None and pin in self._by_id and pin in ids:
                chosen = self._by_id[pin]
            if chosen is None:
                return PresenceConnectionSnapshot(
                    health=ConnectionHealth.AMBIGUOUS,
                    generation=None,
                    connection_id=None,
                    gateway_instance_id=self.gateway_instance_id,
                    live_count=live_count,
                )
            return PresenceConnectionSnapshot(
                health=ConnectionHealth.CONNECTED,
                generation=chosen.generation,
                connection_id=chosen.connection_id,
                gateway_instance_id=chosen.gateway_instance_id,
                live_count=live_count,
            )

    def has_any_active(self) -> bool:
        with self._lock:
            return any(item.healthy and item.bot is not None for item in self._by_id.values())

    def has_unique_account(self, platform: str, external_account_id: str) -> bool:
        try:
            self.resolve_account(platform, external_account_id)
        except RegistryClosed:
            return False
        return True

    def _bind_locked(self, account: tuple[str, str], presence_id: str) -> None:
        previous = self._account_presence.get(account)
        self._account_presence[account] = presence_id
        for connection_id in list(self._by_account.get(account, ())):
            live = self._by_id.get(connection_id)
            if live is None:
                continue
            if live.presence_id == presence_id:
                continue
            if live.presence_id is not None:
                old = self._by_presence.get(live.presence_id, [])
                self._by_presence[live.presence_id] = [
                    item for item in old if item != connection_id
                ]
            live.presence_id = presence_id
            self._index_presence(presence_id, connection_id)
        self._refresh_pin(presence_id)
        if previous and previous != presence_id:
            self._refresh_pin(previous)

    def _index_presence(self, presence_id: str, connection_id: str) -> None:
        ids = self._by_presence.setdefault(presence_id, [])
        if connection_id not in ids:
            ids.append(connection_id)

    def _refresh_pin(self, presence_id: str) -> None:
        ids = self._live_ids_for_presence(presence_id)
        if len(ids) == 1:
            self._pins[presence_id] = ids[0]
        elif self._pins.get(presence_id) not in ids:
            self._pins.pop(presence_id, None)

    def _live_ids_for_presence(self, presence_id: str) -> list[str]:
        return [
            item
            for item in self._by_presence.get(presence_id, ())
            if item in self._by_id
            and self._by_id[item].healthy
            and self._by_id[item].bot is not None
        ]

    def _live_ids_for_account(self, account: tuple[str, str]) -> list[str]:
        return [
            item
            for item in self._by_account.get(account, ())
            if item in self._by_id
            and self._by_id[item].healthy
            and self._by_id[item].bot is not None
        ]

    def _resolve_ids(
        self,
        ids: list[str],
        *,
        pin_key: str | None,
    ) -> ConnectionResolution:
        if not ids:
            raise RegistryClosed("disconnected")
        pin = self._pins.get(pin_key or "")
        chosen_id: str | None = None
        if len(ids) == 1:
            chosen_id = ids[0]
            if pin_key:
                self._pins[pin_key] = chosen_id
        elif pin is not None and pin in ids:
            chosen_id = pin
        if chosen_id is None:
            raise RegistryClosed("ambiguous")
        live = self._by_id[chosen_id]
        if live.bot is None or not live.healthy:
            raise RegistryClosed("disconnected")
        return ConnectionResolution(snapshot=self._snapshot(live), bot=live.bot)

    @staticmethod
    def _snapshot(live: _LiveConnection) -> ConnectionSnapshot:
        return ConnectionSnapshot(
            connection_id=live.connection_id,
            gateway_instance_id=live.gateway_instance_id,
            provider=live.provider,
            platform=live.platform,
            external_account_id=live.external_account_id,
            presence_id=live.presence_id,
            generation=live.generation,
            healthy=live.healthy,
            capabilities=live.capabilities,
        )


def require_capability(resolution: ConnectionResolution, capability: str) -> None:
    if capability not in resolution.snapshot.capabilities:
        raise RegistryClosed("capability")


def as_bot(resolution: ConnectionResolution) -> Any:
    return resolution.bot


_PROCESS_REGISTRY: GatewayConnectionRegistry | None = None


def configure_process_registry(registry: GatewayConnectionRegistry | None) -> None:
    global _PROCESS_REGISTRY
    _PROCESS_REGISTRY = registry


def process_registry() -> GatewayConnectionRegistry | None:
    return _PROCESS_REGISTRY
