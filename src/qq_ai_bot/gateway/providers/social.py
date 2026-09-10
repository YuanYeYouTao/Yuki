"""Explicit OneBot social operations; never expose arbitrary actions to the model."""

from __future__ import annotations

from typing import Any, cast


class OneBotSocialOperations:
    async def send_message(self, handle: object, **params: Any) -> Any:
        action = "send_group_msg" if "group_id" in params else "send_private_msg"
        return await cast(Any, handle).call_api(action, **params)

    async def send_file(self, handle: object, **params: Any) -> Any:
        action = "upload_group_file" if "group_id" in params else "upload_private_file"
        return await cast(Any, handle).call_api(action, **params)

    async def poke(self, handle: object, **params: Any) -> Any:
        return await cast(Any, handle).call_api("send_poke", **params)

    async def members(self, handle: object, **params: Any) -> Any:
        return await cast(Any, handle).call_api("get_group_member_list", **params)

    async def member(self, handle: object, **params: Any) -> Any:
        return await cast(Any, handle).call_api("get_group_member_info", **params)

    async def recall(self, handle: object, **params: Any) -> Any:
        return await cast(Any, handle).call_api("delete_msg", **params)

    async def social_action(self, handle: object, action: str, params: dict[str, Any]) -> Any:
        operations = {
            "send_private_msg": self.send_message,
            "send_group_msg": self.send_message,
            "upload_private_file": self.send_file,
            "upload_group_file": self.send_file,
            "send_poke": self.poke,
            "get_group_member_list": self.members,
            "get_group_member_info": self.member,
            "delete_msg": self.recall,
        }
        if action not in operations:
            raise ValueError("capability_unavailable")
        return await operations[action](handle, **params)
