"""Transport-neutral social operation contracts."""

from enum import StrEnum
from typing import Literal
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


class SocialMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(default="", max_length=4000, repr=False)
    artifact_id: UUID | None = None
    attachment_kind: Literal["image", "file"] | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "SocialMessage":
        if not self.text.strip() and self.artifact_id is None:
            raise ValueError("message_empty")
        if (self.artifact_id is None) != (self.attachment_kind is None):
            raise ValueError("attachment_kind_required")
        return self


class SocialReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    action: str
    status: OperationStatus
    target: SocialTarget
    presence_id: str | None = None
    platform_reference: str | None = None
    error_category: str | None = None
