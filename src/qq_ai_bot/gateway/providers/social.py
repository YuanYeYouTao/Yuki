"""Explicit OneBot social operations; never expose arbitrary actions to the model."""

from __future__ import annotations

from typing import Any, cast


class OneBotSocialOperations:
    async def social_action(self, handle: object, action: str, params: dict[str, Any]) -> Any:
        if action not in {
            "get_group_list",
            "send_private_msg",
            "send_group_msg",
            "upload_private_file",
            "upload_group_file",
            "send_poke",
            "get_group_member_list",
            "get_group_member_info",
            "delete_msg",
            "get_friend_msg_history",
            "get_group_msg_history",
            "get_forward_msg",
        }:
            raise ValueError("capability_unavailable")
        return await cast(Any, handle).call_api(action, **params)
