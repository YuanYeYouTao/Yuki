"""``find_contacts`` arguments: subjects with their entries and original conversations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yuki.services.history_query import HistoryTarget


class ContactsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=128)
    target: HistoryTarget | None = None
    type: Literal["person", "group"] | None = None
    platform: str | None = None
    limit: int = Field(default=20, gt=0, strict=True)
    cursor: str | None = Field(default=None, pattern=r"^(person|group):[0-9a-fA-F-]{36}$")

    @model_validator(mode="after")
    def no_receipt_target(self) -> "ContactsQuery":
        if self.target is not None and self.target.operation_id is not None:
            raise ValueError("target_operation_unsupported")
        return self
