"""QQ Open Platform Bot HTTP boundary; AppSecret and access tokens stay here.

Platform facts follow SuInk/Diana (used with its author's permission): the token
endpoint, API bases and refusal codes below are what its QQ official channel uses.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from yuki.domain.messages import InboundMessage
from yuki.gateway.provider import GatewayConnectionProfile
from yuki.gateway.social import PlatformSocialOperations, TransportTarget

if TYPE_CHECKING:
    from yuki.persistence.database import Database

QQ_OFFICIAL_PROVIDER_ID = "qq_official"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.bot.qq.com"
# The sandbox has no announced api.bot.qq.com host; Diana keeps the older one.
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"


class QQOfficialAPIError(RuntimeError):
    """Safe protocol facts only: never the AppSecret, token or response text."""

    def __init__(
        self,
        code: str,
        *,
        uncertain: bool = False,
        status_code: int | None = None,
        error_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.uncertain = code, uncertain
        self.status_code, self.error_code = status_code, error_code


class QQOfficialReplyLimit(QQOfficialAPIError):
    """40034128: the passive reply window or count of this msg_id is used up."""


class QQOfficialProactiveRefused(QQOfficialAPIError):
    """40034105/40034102: the platform refuses a message without a passive msg_id."""


class QQOfficialNoDeliveryPath(QQOfficialAPIError):
    """No passive msg_id exists for a group; groups have no proactive messages."""


_REFUSALS: dict[int, type[QQOfficialAPIError]] = {
    40034128: QQOfficialReplyLimit,
    40034105: QQOfficialProactiveRefused,
    40034102: QQOfficialProactiveRefused,
}


class QQOfficialClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        sandbox: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id, self._app_secret = app_id, app_secret
        self.api_base = SANDBOX_API_BASE if sandbox else API_BASE
        self._client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self._owns_client = client is None
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        # The Bot's own user id from the gateway READY event.
        self.self_id = ""

    async def access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = await self._client.post(
                    TOKEN_URL, json={"appId": self.app_id, "clientSecret": self._app_secret}
                )
                payload = response.json()
            except httpx.RequestError:
                raise QQOfficialAPIError("qq_official_token_unreachable") from None
            except ValueError:
                raise QQOfficialAPIError(
                    "qq_official_invalid_token_response", status_code=response.status_code
                ) from None
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                code = payload.get("code") if isinstance(payload, dict) else None
                raise QQOfficialAPIError(
                    "qq_official_credentials_rejected",
                    status_code=response.status_code,
                    error_code=code if isinstance(code, int) else None,
                )
            self._token = token
            self._token_expires_at = time.monotonic() + int(payload["expires_in"])
            return token

    def invalidate_token(self, token: str | None = None) -> None:
        """Drop only the rejected token; a concurrently refreshed one stays."""
        if token is None or self._token == token:
            self._token = ""

    async def call(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One authenticated attempt; a rejected write is never replayed here."""
        token = await self.access_token()
        try:
            response = await self._client.request(
                method,
                self.api_base + path,
                json=json,
                headers={"Authorization": f"QQBot {token}"},
            )
        except httpx.RequestError:
            raise QQOfficialAPIError(
                "qq_official_transport_unknown", uncertain=method.upper() != "GET"
            ) from None
        if response.status_code == 401:
            self.invalidate_token(token)
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            raise QQOfficialAPIError(
                "qq_official_invalid_response",
                uncertain=response.is_server_error,
                status_code=response.status_code,
            ) from None
        if not isinstance(payload, dict):
            raise QQOfficialAPIError(
                "qq_official_invalid_response", uncertain=True, status_code=response.status_code
            )
        codes = [
            value
            for value in (payload.get("err_code"), payload.get("code"))
            if isinstance(value, int) and value
        ]
        if codes or response.is_error:
            known = next((value for value in codes if value in _REFUSALS), None)
            raise _REFUSALS.get(known or 0, QQOfficialAPIError)(
                "qq_official_rejected",
                # A coded refusal is definite; a bare server error may have been applied.
                uncertain=not codes and response.is_server_error,
                status_code=response.status_code,
                error_code=known or (codes[0] if codes else None),
            )
        return payload

    async def gateway_url(self) -> str:
        return str((await self.call("GET", "/gateway"))["url"])

    async def put_part(self, url: str, chunk: bytes) -> None:
        """Presigned storage URLs carry their own signature, never the Bot token."""
        try:
            response = await self._client.put(url, content=chunk)
        except httpx.RequestError:
            raise QQOfficialAPIError("qq_official_upload_unreachable") from None
        if response.is_error:
            raise QQOfficialAPIError(
                "qq_official_upload_rejected", status_code=response.status_code
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def verify_qq_official_credentials(
    app_id: str,
    app_secret: str,
    *,
    sandbox: bool = False,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Preflight new credentials: exchange a token and read the gateway with it.

    Raises :class:`QQOfficialAPIError` (``error_code`` from the platform) when refused.
    """
    probe = QQOfficialClient(app_id, app_secret, sandbox=sandbox, client=client)
    try:
        await probe.gateway_url()
    finally:
        await probe.close()


class QQOfficialConnection:
    """Registry handle: the live client plus the durable delivery records it needs."""

    def __init__(self, client: QQOfficialClient, database: Database, presence_id: str = "") -> None:
        self.client, self.database, self.presence_id = client, database, presence_id


class QQOfficialProvider:
    provider_id = QQ_OFFICIAL_PROVIDER_ID
    platform = QQ_OFFICIAL_PROVIDER_ID

    def media(self, handle: object) -> None:
        # Inbound attachments carry their own download URL; nothing to resolve here.
        return None

    def activity(self, handle: object, target: TransportTarget) -> None:
        return None

    def project_inbound(self, message: InboundMessage, yuki_ids: frozenset[str]) -> InboundMessage:
        return message

    @property
    def social(self) -> PlatformSocialOperations:
        from yuki.gateway.providers.qq_official_social import QQOfficialSocialOperations

        return QQOfficialSocialOperations()

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if not isinstance(handle, QQOfficialConnection):
            raise TypeError("QQ official Bot requires its authenticated connection")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform=self.platform,
            external_account_id=handle.client.self_id,
            capabilities=frozenset({"send_private", "send_group", "group_member_probe"}),
        )
