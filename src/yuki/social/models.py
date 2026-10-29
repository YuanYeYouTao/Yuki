"""Transport-neutral social operation contracts."""

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SocialError(RuntimeError):
    """Stable error category, with no raw gateway response attached."""


class OperationStatus(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SocialTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["person", "space"]
    id: UUID


class SocialEmoji(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str
    emotion: str = ""


class SocialMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(default="", repr=False)
    artifact_id: UUID | None = None
    path: str | None = None
    device_id: str | None = None
    name: str | None = None
    expected_version: str | None = None
    attachment_kind: Literal["image", "file"] | None = None
    emoji: SocialEmoji | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "SocialMessage":
        if (
            not self.text.strip()
            and self.artifact_id is None
            and self.path is None
            and self.emoji is None
        ):
            raise ValueError("message_empty")
        if self.artifact_id is not None and self.path is not None:
            raise ValueError("choose_path_or_artifact_id")
        has_attachment = self.artifact_id is not None or self.path is not None
        if has_attachment != (self.attachment_kind is not None):
            raise ValueError("attachment_kind_required")
        if has_attachment and self.emoji is not None:
            raise ValueError("message_media_conflict")
        return self


class SocialReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    action: str
    status: OperationStatus
    target: SocialTarget
    presence_id: str | None = None
    platform_reference: str | None = None
    event_id: int | None = None
    original_event_id: int | None = None
    original_conversation_id: str | None = None
    error_category: str | None = None
    # Local proof carried from preparation to its dispatch CAS, never model output.
    payload_hash: str | None = Field(default=None, exclude=True)
    prepared: dict[str, Any] | None = Field(default=None, exclude=True)
