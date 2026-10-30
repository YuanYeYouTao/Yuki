"""Separate selectors for current subjects and original Conversation discovery."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yuki.services.history_query import HistorySource, HistoryTarget


class SubjectContactsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["person", "space"]
    target_id: UUID | None = Field(default=None, description="已知主体的内部 Person/Space ID")
    subject_ref: str | None = Field(
        default=None,
        description="当前发送者用 current_speaker；也可用 mentioned_user 或 replied_message_author",
    )
    display_name: str = ""
    source: HistorySource | None = None
    limit: int = Field(default=20, gt=0, strict=True)
    cursor: UUID | None = None

    @model_validator(mode="after")
    def one_selector(self) -> "SubjectContactsQuery":
        if sum(bool(value) for value in (self.target_id, self.subject_ref, self.display_name)) > 1:
            raise ValueError("contact subject requires one selector")
        return self


class ConversationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: HistoryTarget | None = None
    source: HistorySource | None = None
    display_name: str = ""
    limit: int = Field(default=20, gt=0, strict=True)
    cursor: UUID | None = None


def contact_schema() -> dict[str, object]:
    return SubjectContactsQuery.model_json_schema()


def conversation_schema() -> dict[str, object]:
    return ConversationQuery.model_json_schema()
