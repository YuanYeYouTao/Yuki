"""SnowLuma's private-chat input status, without message effects or receipts."""

from typing import Any, Protocol


class OneBotActivityHandle(Protocol):
    async def call_api(self, api: str, **params: Any) -> Any: ...


class OneBotTypingActivity:
    refresh_interval_seconds = 4.0

    def __init__(self, handle: OneBotActivityHandle, user_id: str) -> None:
        self._handle = handle
        self._user_id = int(user_id)

    async def start(self) -> None:
        await self._handle.call_api("set_input_status", user_id=self._user_id, event_type=1)

    async def stop(self) -> None:
        await self._handle.call_api("set_input_status", user_id=self._user_id, event_type=0)
