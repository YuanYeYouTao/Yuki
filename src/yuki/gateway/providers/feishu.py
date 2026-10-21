"""Feishu/Lark native HTTP boundary; credentials and original media stay here."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Mapping
from typing import Any, cast
from urllib.parse import quote

import httpx

from yuki.domain.messages import InboundMessage
from yuki.gateway.media import MediaKind, MediaResolutionError, PlatformMediaFile
from yuki.gateway.provider import GatewayConnectionProfile
from yuki.gateway.social import PlatformSocialOperations, TransportTarget


def encode_account_id(app_id: str, open_id: str) -> str:
    """Open IDs belong to one app; canonical accounts retain that namespace."""
    return f"{app_id}:{open_id}"


def decode_account_id(app_id: str, account_id: str) -> str:
    prefix = f"{app_id}:"
    if not account_id.startswith(prefix):
        raise ValueError("feishu_account_app_mismatch")
    return account_id[len(prefix) :]


class FeishuAPIError(RuntimeError):
    """Safe protocol facts, without upstream messages, tokens or signed URLs."""

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


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str = "https://open.feishu.cn",
        platform: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id, self._app_secret = app_id, app_secret
        self.domain = domain.rstrip("/")
        if platform is None:
            try:
                platform = {
                    "https://open.feishu.cn": "feishu",
                    "https://open.larksuite.com": "lark",
                }[self.domain]
            except KeyError:
                raise ValueError("feishu_domain_requires_platform") from None
        self.platform = platform
        self._client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self._owns_client = client is None
        self._tenant_token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self.self_id = ""
        self.name = ""

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        token: str | None = None,
        uncertain: bool,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            if files is None:
                response = await self._client.request(
                    method, self.domain + path, params=params, json=json, headers=headers
                )
            else:
                response = await self._client.request(
                    method,
                    self.domain + path,
                    params=params,
                    data=json,
                    files=files,
                    headers=headers,
                )
        except httpx.RequestError:
            raise FeishuAPIError("feishu_transport_unknown", uncertain=uncertain) from None
        try:
            payload = response.json()
        except ValueError:
            raise FeishuAPIError(
                "feishu_invalid_response", uncertain=uncertain, status_code=response.status_code
            ) from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise FeishuAPIError(
                "feishu_invalid_response", uncertain=uncertain, status_code=response.status_code
            )
        if payload["code"] != 0:
            raise FeishuAPIError(
                "feishu_rejected",
                status_code=response.status_code,
                error_code=payload["code"],
            )
        if response.is_error or response.is_redirect:
            raise FeishuAPIError(
                "feishu_invalid_response", uncertain=uncertain, status_code=response.status_code
            )
        return payload

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._tenant_token and time.monotonic() < self._token_expires_at:
                return self._tenant_token
            payload = await self._request_json(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self._app_secret},
                uncertain=False,
            )
            token, expires = payload.get("tenant_access_token"), payload.get("expire")
            if not isinstance(token, str) or not token or type(expires) is not int:
                raise FeishuAPIError("feishu_invalid_token_response")
            self._tenant_token = token
            self._token_expires_at = time.monotonic() + expires
            return token

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """One authenticated API attempt; writes are never refreshed and replayed."""
        token = await self._access_token()
        try:
            return await self._request_json(
                method,
                path,
                params=params,
                json=json,
                files=files,
                token=token,
                uncertain=method.upper() not in {"GET", "HEAD"},
            )
        except FeishuAPIError as exc:
            # Invalidate only this rejected credential. A later independent call may
            # obtain a new token; the current write is never automatically repeated.
            if exc.error_code in {99991663, 99991664, 99991665} and self._tenant_token == token:
                self._tenant_token = ""
            raise

    async def websocket_endpoint(self) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            "/callback/ws/endpoint",
            json={"AppID": self.app_id, "AppSecret": self._app_secret},
            uncertain=False,
        )

    async def get_me(self) -> dict[str, Any]:
        payload = await self.call("GET", "/open-apis/bot/v3/info")
        bot = payload.get("bot")
        if (
            not isinstance(bot, dict)
            or not isinstance(bot.get("open_id"), str)
            or not bot["open_id"]
        ):
            raise FeishuAPIError("feishu_invalid_identity")
        self.self_id = encode_account_id(self.app_id, bot["open_id"])
        self.name = str(bot.get("app_name") or "")
        return bot

    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int | None
    ) -> PlatformMediaFile:
        del kind
        try:
            resource = json.loads(file)
            message_id, resource_key, resource_type = (
                resource["message_id"],
                resource["resource_key"],
                resource["type"],
            )
            if resource_type not in {"image", "file"}:
                raise ValueError("unsupported_resource")
            path = (
                f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/resources/"
                f"{quote(resource_key, safe='')}"
            )
        except (ValueError, KeyError, TypeError):
            raise MediaResolutionError("resource_unavailable", "飞书媒体缺少原消息定位") from None
        return PlatformMediaFile(
            chunks=lambda: self._media_chunks(path, resource_type, max_download_bytes)
        )

    async def _media_chunks(
        self, path: str, resource_type: str, max_download_bytes: int | None
    ) -> AsyncGenerator[bytes, None]:
        try:
            token = await self._access_token()
            async with self._client.stream(
                "GET",
                self.domain + path,
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.is_error or response.is_redirect:
                    raise MediaResolutionError("download_failed", "飞书媒体下载失败")
                length = response.headers.get("content-length")
                if (
                    max_download_bytes is not None
                    and length is not None
                    and int(length) > max_download_bytes
                ):
                    raise MediaResolutionError("too_large", "飞书媒体超过下载预算")
                size = 0
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if max_download_bytes is not None and size > max_download_bytes:
                        raise MediaResolutionError("too_large", "飞书媒体超过下载预算")
                    yield chunk
        except (httpx.RequestError, FeishuAPIError):
            raise MediaResolutionError("download_failed", "飞书媒体下载失败") from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class FeishuProvider:
    def __init__(self, platform: str = "feishu") -> None:
        self.platform = self.provider_id = platform

    def media(self, handle: object) -> FeishuClient:
        return cast(FeishuClient, handle)

    def activity(self, handle: object, target: TransportTarget) -> None:
        return None

    def project_inbound(self, message: InboundMessage, yuki_ids: frozenset[str]) -> InboundMessage:
        return message

    @property
    def social(self) -> PlatformSocialOperations:
        from yuki.gateway.providers.feishu_social import FeishuSocialOperations

        return FeishuSocialOperations()

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        if not isinstance(handle, FeishuClient) or handle.platform != self.platform:
            raise TypeError("Feishu requires its authenticated native client and cloud")
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform=self.platform,
            external_account_id=handle.self_id,
            capabilities=frozenset(
                {"send_private", "send_group", "group_member_probe", "media_fetch"}
            ),
        )
