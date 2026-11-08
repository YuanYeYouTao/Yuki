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


class SocialSticker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str
    emotion: str = ""


class SocialFile(BaseModel):
    """One original file: a path (optionally on a personal computer) or a snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str | None = None
    artifact_id: UUID | None = None
    device: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> "SocialFile":
        if (self.path is None) == (self.artifact_id is None):
            raise ValueError("choose_one_source")
        if self.device is not None and self.path is None:
            raise ValueError("device_requires_path")
        return self


class SocialMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(default="", repr=False)
    file: SocialFile | None = None
    # Images display as chat images unless sent as a file; other files are files.
    as_file: bool = False
    sticker: SocialSticker | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "SocialMessage":
        if not self.text.strip() and self.file is None and self.sticker is None:
            raise ValueError("message_empty")
        if self.file is not None and self.sticker is not None:
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
