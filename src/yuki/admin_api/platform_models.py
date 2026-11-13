"""Typed platform credential state; secret values never appear in these models."""

from typing import Literal

from pydantic import BaseModel


class PlatformAccount(BaseModel):
    presence_id: str
    external_account_id: str
    display_name: str


class PlatformHealthView(BaseModel):
    enabled: bool
    running: bool
    healthy: bool | None
    connected: bool
    last_error_category: str | None
    last_success_at: str | None
    accounts: list[PlatformAccount]


class PlatformView(BaseModel):
    configured: bool
    enabled: bool
    settings: dict[str, str | bool]
    saved_secrets: list[str]
    apply_mode: Literal["hot", "restart"]
    health: PlatformHealthView


class OneBotPlatformView(BaseModel):
    """SnowLuma stays in the deployment .env; this API only reports it."""

    apply_mode: Literal["restart"]
    savable: Literal[False]
    login_available: bool
    health: PlatformHealthView


class PlatformEntries(BaseModel):
    telegram: PlatformView
    feishu: PlatformView
    lark: PlatformView
    qq_official: PlatformView
    qq: OneBotPlatformView


class PlatformsView(BaseModel):
    revision: int
    writable_directory: bool
    platforms: PlatformEntries
