"""Explicit non-chat model connections in the existing configuration bundle."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yuki.domain.audio import ASRProtocol


class _ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""

    @model_validator(mode="after")
    def _configured(self) -> _ModelConnection:
        if self.enabled and (not self.base_url or not self.model):
            raise ValueError("enabled model connection requires a base URL and model")
        return self


class EmbeddingConnection(_ModelConnection):
    provider: Literal["qwen_dashscope", "openai_compatible"] = "qwen_dashscope"
    dimensions: int = Field(default=1024, gt=0)
    output_type: Literal["dense"] = "dense"
    document_template_version: int = Field(default=1, gt=0)
    query_instruct: str = "Retrieve personal memory facts relevant to the conversational query."


class VisionConnection(_ModelConnection):
    protocol: Literal["chat_completions"] = "chat_completions"


class ASRConnection(_ModelConnection):
    protocol: ASRProtocol = "chat_completions"


class SearchConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tavily_api_key_env: str = ""


CONNECTION_MODELS: dict[
    str,
    type[EmbeddingConnection]
    | type[VisionConnection]
    | type[ASRConnection]
    | type[SearchConnection],
] = {
    "embedding": EmbeddingConnection,
    "vision": VisionConnection,
    "asr": ASRConnection,
    "search": SearchConnection,
}
