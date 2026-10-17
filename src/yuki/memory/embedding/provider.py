"""Embedding provider protocol and stable public error taxonomy."""

from __future__ import annotations

from typing import Protocol

from yuki.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingProviderCapabilities,
    EmbeddingProviderProfile,
)


class EmbeddingProviderError(RuntimeError):
    """A classified provider failure containing no remote response or credentials."""

    def __init__(self, code: str, public_message: str, *, retryable: bool) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable


class EmbeddingProvider(Protocol):
    @property
    def profile(self) -> EmbeddingProviderProfile: ...

    @property
    def capabilities(self) -> EmbeddingProviderCapabilities: ...

    async def embed_documents(self, texts: tuple[str, ...]) -> EmbeddingBatchResult: ...

    async def embed_query(self, text: str) -> EmbeddingBatchResult: ...

    async def close(self) -> None: ...
