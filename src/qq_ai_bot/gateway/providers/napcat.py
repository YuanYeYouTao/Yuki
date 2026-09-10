"""NapCat implementation of the provider-neutral OneBot gateway contract."""

from __future__ import annotations

from typing import Final, final

from qq_ai_bot.gateway.provider import (
    GatewayConnectionProfile,
    GatewayProviderCatalog,
)
from qq_ai_bot.gateway.providers.social import OneBotSocialOperations

NAPCAT_PROVIDER_ID: Final[str] = "napcat"
NAPCAT_PLATFORM: Final[str] = "qq"
NAPCAT_CAPABILITIES: Final[frozenset[str]] = frozenset(
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
class NapCatProvider(OneBotSocialOperations):
    """Profile NapCat OneBot handles without leaking them into core services."""

    @property
    def provider_id(self) -> str:
        return NAPCAT_PROVIDER_ID

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if handle is None:
            raise TypeError("gateway handle is required")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform=NAPCAT_PLATFORM,
            external_account_id=str(getattr(handle, "self_id", "")),
            capabilities=NAPCAT_CAPABILITIES,
        )


def napcat_provider_catalog() -> GatewayProviderCatalog:
    """Return a single-provider catalog for focused NapCat consumers and tests."""

    return GatewayProviderCatalog((NapCatProvider(),))
