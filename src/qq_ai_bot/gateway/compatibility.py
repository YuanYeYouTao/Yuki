"""Secret-free OneBot v11 contracts for built-in QQ gateway Providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final, Literal, final

from qq_ai_bot.gateway.providers.napcat import NAPCAT_PROVIDER_ID
from qq_ai_bot.gateway.providers.snowluma import SNOWLUMA_PROVIDER_ID


@final
@dataclass(frozen=True, slots=True)
class OneBotActionContract:
    """One action Yuki itself relies on across supported Providers."""

    action: str
    effect: Literal["read", "send"]
    provider_capability: str
    required_parameters: tuple[str, ...]


CORE_ONEBOT_ACTIONS: Final[tuple[OneBotActionContract, ...]] = (
    OneBotActionContract("send_private_msg", "send", "send_private", ("user_id", "message")),
    OneBotActionContract("send_group_msg", "send", "send_group", ("group_id", "message")),
    OneBotActionContract("get_group_info", "read", "profile_lookup", ("group_id",)),
    OneBotActionContract(
        "get_group_member_info",
        "read",
        "group_member_probe",
        ("group_id", "user_id"),
    ),
    OneBotActionContract("get_stranger_info", "read", "profile_lookup", ("user_id",)),
    OneBotActionContract("get_image", "read", "media_fetch", ("file",)),
    OneBotActionContract(
        "get_group_msg_history",
        "read",
        "message_history",
        ("group_id", "count"),
    ),
    OneBotActionContract(
        "get_friend_msg_history",
        "read",
        "message_history",
        ("user_id", "count"),
    ),
)

_REVERSE_WS_PATHS: Final[dict[str, tuple[str, ...]]] = {
    NAPCAT_PROVIDER_ID: ("/onebot/v11/", "/onebot/v11/ws"),
    SNOWLUMA_PROVIDER_ID: ("/onebot/v11/snowluma/ws",),
}


def provider_doctor_payload(provider_id: str) -> dict[str, object]:
    """Describe the cross-Provider contract without issuing a live or private API call."""

    normalized = provider_id.strip().casefold()
    paths = _REVERSE_WS_PATHS.get(normalized)
    if paths is None:
        raise ValueError("unknown gateway provider")
    return {
        "provider_id": normalized,
        "protocol": "onebot_v11",
        "reverse_ws_paths": list(paths),
        "core_actions": [asdict(item) for item in CORE_ONEBOT_ACTIONS],
        "contract_status": "declared",
        "live_probe": "not_run",
        "provider_private_actions": "not_guaranteed",
    }
