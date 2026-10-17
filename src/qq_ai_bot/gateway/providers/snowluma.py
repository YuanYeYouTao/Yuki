"""SnowLuma implementation of the provider-neutral OneBot gateway contract."""

from __future__ import annotations

from pathlib import Path
from typing import Final, cast, final

from qq_ai_bot.adapters.onebot.activity import OneBotActivityHandle, OneBotTypingActivity
from qq_ai_bot.adapters.onebot.media import OneBotMediaAdapter, OneBotMediaHandle
from qq_ai_bot.adapters.onebot.social import OneBotSocialOperations
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.gateway.provider import GatewayConnectionProfile
from qq_ai_bot.gateway.social import TransportTarget

SNOWLUMA_PROVIDER_ID: Final[str] = "snowluma"
SNOWLUMA_PLATFORM: Final[str] = "qq"
SNOWLUMA_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "send_private",
        "send_group",
        "group_member_probe",
        "profile_lookup",
        "message_history",
        "media_fetch",
        "typing_private",
    }
)


@final
class SnowLumaProvider:
    """Profile SnowLuma OneBot handles without leaking them into core services."""

    def __init__(self, transfer_root: Path | None = None, gateway_root: str = "") -> None:
        self.social = OneBotSocialOperations(transfer_root, gateway_root)

    def media(self, handle: object) -> OneBotMediaAdapter:
        return OneBotMediaAdapter(cast(OneBotMediaHandle, handle))

    def activity(self, handle: object, target: TransportTarget) -> OneBotTypingActivity | None:
        if target.kind != "person":
            return None
        return OneBotTypingActivity(cast(OneBotActivityHandle, handle), target.external_id)

    def project_inbound(self, message: InboundMessage, yuki_ids: frozenset[str]) -> InboundMessage:
        from qq_ai_bot.adapters.onebot.normalizer import reproject_inbound_mentions

        return reproject_inbound_mentions(message, yuki_ids)

    @property
    def provider_id(self) -> str:
        return SNOWLUMA_PROVIDER_ID

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if handle is None:
            raise TypeError("gateway handle is required")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform=SNOWLUMA_PLATFORM,
            external_account_id=str(getattr(handle, "self_id", "")),
            capabilities=SNOWLUMA_CAPABILITIES,
        )
