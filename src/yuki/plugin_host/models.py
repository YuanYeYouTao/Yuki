"""Host-only plugin discovery, approval, and lifecycle projections."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.permissions import PluginPermission


class PluginStatus(StrEnum):
    DISCOVERED = "discovered"
    INVALID = "invalid"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    DISABLED = "disabled"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"


class PluginApprovalRecord(StrictModel):
    plugin_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_permissions: tuple[PluginPermission, ...]
    approved_by: str = Field(min_length=1, max_length=64)
    approved_at: datetime


class PluginDiscoveryRecord(StrictModel):
    directory: Path
    plugin_id: str | None = None
    status: PluginStatus
    error_category: str | None = None
    detail: str = Field(default="", max_length=500)
