"""Versioned HTTP shapes; executor authority never accepts a physical host path."""

from typing import Any, Literal
from uuid import uuid4

from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1)
    digest: str = Field(pattern=r"^(sha256:)?[0-9a-f]{64}$")


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(min_length=1)
    scope: Scope | None = None

    @model_validator(mode="after")
    def scoped_capability(self) -> "Grant":
        if self.capability in {"folder.read", "folder.write", "open.file"} and self.scope is None:
            raise ValueError("scope_required")
        return self


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4 = Field(default_factory=uuid4)


class CreateExecutor(Command):
    request_id: UUID4
    label: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=64)


class ReplaceGrants(Command):
    request_id: UUID4
    expected_revision: int = Field(strict=True, ge=1)
    grants: list[Grant]

    @model_validator(mode="after")
    def unique_scopes(self) -> "ReplaceGrants":
        keys = [(g.capability, g.scope.alias if g.scope else "") for g in self.grants]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_grant")
        return self


class ExecutorConfig(BaseModel):
    executor_id: str
    paused: bool
    revision: int
    grants_digest: str
    grants: list[Grant]


class ExecutorView(ExecutorConfig):
    label: str
    platform: str
    online: bool
    last_seen_at: str | None
    claimed: bool


class ExecutorCreated(BaseModel):
    executor_id: str
    label: str
    platform: str
    created_at: str
    token: str | None = None
    replay: bool


class ExecutorOperation(BaseModel):
    op_id: str
    capability: str
    params: dict[str, Any]
    state: Literal[
        "registered",
        "dispatched",
        "acknowledged",
        "awaiting_user",
        "succeeded",
        "failed",
        "unknown",
    ]
    level: Literal["confirmed", "submitted"] | None
    result: dict[str, Any]
    observation: dict[str, Any] | None
    created_at: str
    dispatched_at: str | None
    acknowledged_at: str | None
    settled_at: str | None


class OperationPage(BaseModel):
    items: list[ExecutorOperation]
    next_cursor: str | None


class OperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["succeeded", "failed", "awaiting_user", "unknown"]
    level: Literal["confirmed", "submitted"] | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] | None = None


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grants_digest: str
    local_paused: bool = Field(strict=True)


class HeartbeatResult(BaseModel):
    grants_digest: str
    paused: bool


class ExecutorSync(BaseModel):
    pending: list[str]
    grants_digest: str


class ExecutorOperationHint(BaseModel):
    op_id: str


class ExecutorConfigHint(BaseModel):
    grants_digest: str
