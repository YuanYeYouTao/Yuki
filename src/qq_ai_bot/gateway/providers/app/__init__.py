"""First-party local chat gateway. Social remains the sole outbound writer."""

from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.gateway.local_clients import local_account_id
from qq_ai_bot.gateway.provider import GatewayConnectionProfile
from qq_ai_bot.gateway.providers.app.social import AppSocialOperations
from qq_ai_bot.gateway.social import TransportTarget

APP_PROVIDER_ID = "app"


class AppHandle:
    self_id = "yuki-app"

    def __init__(self, platform: str = "app") -> None:
        self.platform = platform
        self.self_id = local_account_id(platform)


class AppProvider:
    def __init__(self, platform: str = "app") -> None:
        self.platform = platform
        self.provider_id = platform

    def media(self, handle: object) -> None:
        return None

    def activity(self, handle: object, target: TransportTarget) -> None:
        return None

    def project_inbound(self, message: InboundMessage, yuki_ids: frozenset[str]) -> InboundMessage:
        return message

    provider_id = APP_PROVIDER_ID
    social = AppSocialOperations()

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if not isinstance(handle, AppHandle):
            raise TypeError("app gateway requires its local handle")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform=self.platform,
            external_account_id=handle.self_id,
            capabilities=frozenset({"send_private"}),
        )
