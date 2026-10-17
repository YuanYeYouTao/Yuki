"""Native Telegram HTTP transport; no OneBot actions or automatic send replay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote

import httpx

from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.gateway.media import MediaKind, MediaResolutionError, PlatformMediaFile
from qq_ai_bot.gateway.provider import GatewayConnectionProfile
from qq_ai_bot.gateway.social import PlatformSocialOperations
from qq_ai_bot.services.http_logging import redact_httpx_url

TELEGRAM_PROVIDER_ID = "telegram"


class TelegramAPIError(RuntimeError):
    """Only safe protocol facts, never the token URL or server description."""

    def __init__(
        self,
        code: str,
        *,
        uncertain: bool = False,
        status_code: int | None = None,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.uncertain = code, uncertain
        self.status_code, self.error_code, self.retry_after = status_code, error_code, retry_after


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base_url: str = "https://api.telegram.org",
    ) -> None:
        self._token = token
        self._base_url = api_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self._owns_client = client is None
        self.self_id = ""
        self.username = ""

    async def call(
        self,
        method: str,
        params: Mapping[str, Any],
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        try:
            with redact_httpx_url("[telegram-url-redacted]"):
                if files is None:
                    response = await self._client.post(
                        f"{self._base_url}/bot{self._token}/{method}", json=dict(params)
                    )
                else:
                    form = {
                        key: value if isinstance(value, str) else json.dumps(value)
                        for key, value in params.items()
                    }
                    response = await self._client.post(
                        f"{self._base_url}/bot{self._token}/{method}", data=form, files=files
                    )
        except httpx.RequestError:
            raise TelegramAPIError("telegram_transport_unknown", uncertain=True) from None
        try:
            payload = response.json()
        except ValueError:
            raise TelegramAPIError(
                "telegram_invalid_response", uncertain=True, status_code=response.status_code
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise TelegramAPIError(
                "telegram_invalid_response", uncertain=True, status_code=response.status_code
            )
        if not payload["ok"]:
            parameters = payload.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            error_code = payload.get("error_code")
            raise TelegramAPIError(
                "telegram_rejected",
                status_code=response.status_code,
                error_code=error_code if isinstance(error_code, int) else None,
                retry_after=retry_after if isinstance(retry_after, int) else None,
            )
        if "result" not in payload or response.is_error:
            raise TelegramAPIError(
                "telegram_invalid_response", uncertain=True, status_code=response.status_code
            )
        return payload["result"]

    async def get_me(self) -> dict[str, Any]:
        result = await self.call("getMe", {})
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramAPIError("telegram_invalid_identity")
        self.self_id = str(result["id"])
        self.username = str(result.get("username") or "")
        return result

    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int
    ) -> PlatformMediaFile:
        del kind  # getFile resolves the original platform file for every supported media kind.
        try:
            metadata = await self.call("getFile", {"file_id": file})
        except TelegramAPIError:
            raise MediaResolutionError("resource_lookup_failed", "Telegram 媒体查询失败") from None
        if not isinstance(metadata, dict) or not isinstance(metadata.get("file_path"), str):
            raise MediaResolutionError("resource_unavailable", "Telegram 媒体缺少下载定位")
        size = metadata.get("file_size")
        if isinstance(size, int) and size > max_download_bytes:
            raise MediaResolutionError("too_large", "Telegram 媒体超过下载预算")
        try:
            with redact_httpx_url("[telegram-url-redacted]"):
                file_path = quote(metadata["file_path"], safe="/")
                async with self._client.stream(
                    "GET",
                    f"{self._base_url}/file/bot{self._token}/{file_path}",
                ) as response:
                    if response.is_error or response.is_redirect:
                        raise MediaResolutionError("download_failed", "Telegram 媒体下载失败")
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_download_bytes:
                            raise MediaResolutionError("too_large", "Telegram 媒体超过下载预算")
                        chunks.append(chunk)
                    return PlatformMediaFile(
                        content=b"".join(chunks), content_type=response.headers.get("content-type")
                    )
        except httpx.RequestError:
            raise MediaResolutionError("download_failed", "Telegram 媒体下载失败") from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class TelegramProvider:
    def media(self, handle: object) -> TelegramClient:
        return cast(TelegramClient, handle)

    def project_inbound(self, message: InboundMessage, yuki_ids: frozenset[str]) -> InboundMessage:
        return message

    @property
    def social(self) -> PlatformSocialOperations:
        from qq_ai_bot.gateway.providers.telegram_social import TelegramSocialOperations

        return TelegramSocialOperations()

    provider_id = TELEGRAM_PROVIDER_ID

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if not isinstance(handle, TelegramClient):
            raise TypeError("Telegram requires its authenticated native client")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform="telegram",
            external_account_id=handle.self_id,
            capabilities=frozenset(
                {
                    "send_private",
                    "send_group",
                    "group_member_probe",
                    "profile_lookup",
                    "media_fetch",
                }
            ),
        )
