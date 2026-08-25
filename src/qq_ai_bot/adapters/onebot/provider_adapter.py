"""Provider-aware OneBot v11 reverse WebSocket adapters."""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection
from typing import ClassVar, Final, override

from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot
from nonebot.drivers import ASGIMixin, WebSocket, WebSocketServerSetup
from yarl import URL

from qq_ai_bot.gateway.providers.napcat import NAPCAT_PROVIDER_ID
from qq_ai_bot.gateway.providers.snowluma import SNOWLUMA_PROVIDER_ID

PROVIDER_CONFLICT_CATEGORY: Final[str] = "provider_conflict"
SNOWLUMA_REVERSE_WS_PATH: Final[str] = "/onebot/v11/snowluma/ws"

logger = logging.getLogger(__name__)


class ProviderConnectionGuard:
    """Reserve one process-wide active OneBot connection per external account."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._claims: dict[str, str] = {}

    def claim(
        self,
        *,
        external_account_id: str,
        provider_id: str,
        connected_account_ids: Collection[str],
    ) -> bool:
        """Claim an account before accepting a socket, or fail without replacing it."""

        account_id = str(external_account_id).strip()
        if not account_id:
            return False
        with self._lock:
            if account_id in connected_account_ids or account_id in self._claims:
                return False
            self._claims[account_id] = provider_id
            return True

    def release(self, *, external_account_id: str, provider_id: str) -> None:
        """Release only the claim owned by the disconnecting Provider."""

        account_id = str(external_account_id).strip()
        with self._lock:
            if self._claims.get(account_id) == provider_id:
                self._claims.pop(account_id, None)


_PROCESS_CONNECTION_GUARD = ProviderConnectionGuard()


class ProviderOneBotAdapter(OneBotV11Adapter):
    """OneBot adapter that rejects duplicate QQ connections before socket acceptance."""

    provider_id: ClassVar[str]

    @override
    async def _handle_ws(self, websocket: WebSocket) -> None:
        self_id = websocket.request.headers.get("x-self-id")
        if not self_id:
            await super()._handle_ws(websocket)
            return
        claimed = _PROCESS_CONNECTION_GUARD.claim(
            external_account_id=self_id,
            provider_id=self.provider_id,
            connected_account_ids=self.driver.bots,
        )
        if not claimed:
            logger.warning(
                "onebot_connection_rejected provider=%s category=%s",
                self.provider_id,
                PROVIDER_CONFLICT_CATEGORY,
            )
            await websocket.close(1008, PROVIDER_CONFLICT_CATEGORY)
            return
        try:
            await super()._handle_ws(websocket)
        finally:
            _PROCESS_CONNECTION_GUARD.release(
                external_account_id=self_id,
                provider_id=self.provider_id,
            )


class NapCatOneBotAdapter(ProviderOneBotAdapter):
    """NapCat adapter retaining every legacy OneBot v11 endpoint."""

    provider_id = NAPCAT_PROVIDER_ID

    @classmethod
    @override
    def get_name(cls) -> str:
        return "OneBot V11 / NapCat"


class SnowLumaOneBotAdapter(ProviderOneBotAdapter):
    """SnowLuma adapter exposing a dedicated reverse WebSocket endpoint."""

    provider_id = SNOWLUMA_PROVIDER_ID
    reverse_ws_paths: ClassVar[tuple[str, ...]] = (
        SNOWLUMA_REVERSE_WS_PATH,
        f"{SNOWLUMA_REVERSE_WS_PATH}/",
    )

    @classmethod
    @override
    def get_name(cls) -> str:
        return "OneBot V11 / SnowLuma"

    @override
    def _setup(self) -> None:
        if isinstance(self.driver, ASGIMixin):
            for index, path in enumerate(self.reverse_ws_paths):
                suffix = "" if index == 0 else " Slash"
                self.setup_websocket_server(
                    WebSocketServerSetup(
                        URL(path),
                        f"{self.get_name()} WS{suffix}",
                        self._handle_ws,
                    )
                )
        self.driver.on_shutdown(self._stop)


def provider_id_for_bot(bot: Bot) -> str:
    """Read trusted Provider provenance from the adapter that owns the Bot handle."""

    provider_id = getattr(bot.adapter, "provider_id", None)
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise RuntimeError("OneBot connection is missing Provider provenance")
    return provider_id.strip().casefold()


__all__ = [
    "PROVIDER_CONFLICT_CATEGORY",
    "SNOWLUMA_REVERSE_WS_PATH",
    "NapCatOneBotAdapter",
    "ProviderConnectionGuard",
    "ProviderOneBotAdapter",
    "SnowLumaOneBotAdapter",
    "provider_id_for_bot",
]
