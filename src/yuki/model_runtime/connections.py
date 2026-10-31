"""Explicit non-chat model connections in the existing configuration bundle."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    provider: Literal["qwen_dashscope", "openai_compatible"]
    base_url: str = Field(min_length=1)
    api_key_env: str = ""
    model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    output_type: Literal["dense"] = "dense"
    document_template_version: int = Field(default=1, gt=0)
    query_instruct: str = "Retrieve personal memory facts relevant to the conversational query."
