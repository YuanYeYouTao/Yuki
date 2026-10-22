"""Contact subjects and original Conversation entries share one directory tool."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from yuki.services.history_query import HistorySource, HistoryTarget


class SubjectContactsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["subjects"] = "subjects"
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


class EntryContactsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["entries"]
    target: HistoryTarget | None = None
    source: HistorySource | None = None
    display_name: str = ""
    limit: int = Field(default=20, gt=0, strict=True)
    cursor: UUID | None = None


CONTACT_QUERY: TypeAdapter[SubjectContactsQuery | EntryContactsQuery] = TypeAdapter(
    Annotated[SubjectContactsQuery | EntryContactsQuery, Field(discriminator="view")]
)


def contact_schema() -> dict[str, object]:
    schema = CONTACT_QUERY.json_schema()
    schema.pop("discriminator")
    subjects = schema["$defs"]["SubjectContactsQuery"]["properties"]
    entries = schema["$defs"]["EntryContactsQuery"]["properties"]
    schema.update(
        type="object",
        properties={
            **subjects,
            **entries,
            "view": {"type": "string", "enum": ["subjects", "entries"], "default": "subjects"},
        },
        additionalProperties=False,
    )
    return schema
