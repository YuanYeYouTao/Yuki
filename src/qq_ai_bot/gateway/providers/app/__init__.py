"""First-party local chat gateway. Social remains the sole outbound writer."""

from qq_ai_bot.gateway.provider import GatewayConnectionProfile

APP_PROVIDER_ID = "app"


class AppHandle:
    self_id = "yuki-app"


class AppProvider:
    provider_id = APP_PROVIDER_ID

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if not isinstance(handle, AppHandle):
            raise TypeError("app gateway requires its local handle")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform="app",
            external_account_id=handle.self_id,
            capabilities=frozenset({"send_private"}),
        )
