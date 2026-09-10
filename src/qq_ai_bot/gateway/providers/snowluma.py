"""SnowLuma implementation of the provider-neutral OneBot gateway contract."""

from __future__ import annotations

from typing import Final, final

from qq_ai_bot.gateway.provider import GatewayConnectionProfile
from qq_ai_bot.gateway.providers.social import OneBotSocialOperations

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
    }
)


@final
class SnowLumaProvider(OneBotSocialOperations):
    """Profile SnowLuma OneBot handles without leaking them into core services."""

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
