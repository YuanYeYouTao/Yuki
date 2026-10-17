"""First-party local chat gateway. Social remains the sole outbound writer."""

from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.gateway.provider import GatewayConnectionProfile
from qq_ai_bot.gateway.providers.app.social import AppSocialOperations

APP_PROVIDER_ID = "app"


class AppHandle:
    self_id = "yuki-app"


class AppProvider:
    def media(self, handle: object) -> None:
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
            platform="app",
            external_account_id=handle.self_id,
            capabilities=frozenset({"send_private"}),
        )
