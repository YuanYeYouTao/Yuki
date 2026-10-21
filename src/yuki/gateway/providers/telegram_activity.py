"""Telegram's expiring typing action on the original chat and topic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yuki.gateway.social import TransportTarget

if TYPE_CHECKING:
    from yuki.gateway.providers.telegram import TelegramClient


class TelegramTypingActivity:
    refresh_interval_seconds = 4.0

    def __init__(self, client: TelegramClient, target: TransportTarget) -> None:
        self._client = client
        self._target = target

    async def start(self) -> None:
        params: dict[str, object] = {"chat_id": self._target.external_id, "action": "typing"}
        if self._target.thread_id is not None:
            params["message_thread_id"] = int(self._target.thread_id)
        await self._client.call("sendChatAction", params)

    async def stop(self) -> None:
        # Telegram clears typing on delivery or expiry; there is no stop action.
        return None
