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


class SocialMention(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: UUID | None = None
    display_name: str | None = Field(default=None, max_length=128)
    subject_ref: str | None = None
    binding_id: UUID | None = None


class SocialVoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    style_hint: str = Field(default="", max_length=128)
    language: Literal["auto", "zh", "jp"] = "auto"
    request_basis: Literal["user_requested", "agent_initiated"]


class SocialEmoji(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str = Field(max_length=300)
    emotion: str = Field(default="", max_length=100)


class SocialMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(default="", max_length=12000, repr=False)
    artifact_id: UUID | None = None
    attachment_kind: Literal["image", "file"] | None = None
    voice: SocialVoice | None = None
    emoji: SocialEmoji | None = None
    mentions: list[SocialMention] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_content(self) -> "SocialMessage":
        if (
            not self.text.strip()
            and self.artifact_id is None
            and self.emoji is None
            and not self.mentions
        ):
            raise ValueError("message_empty")
        if (self.artifact_id is None) != (self.attachment_kind is None):
            raise ValueError("attachment_kind_required")
        if sum((self.artifact_id is not None, self.voice is not None, self.emoji is not None)) > 1:
            raise ValueError("message_media_conflict")
        if self.voice is not None and (not self.text.strip() or self.mentions):
            raise ValueError("voice_text_required")
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
