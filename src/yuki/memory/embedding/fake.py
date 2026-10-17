"""Deterministic non-network embedding provider for tests and local development."""

from __future__ import annotations

import hashlib

from yuki.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingProviderCapabilities,
    EmbeddingProviderProfile,
    EmbeddingUsage,
    EmbeddingVector,
)


class FakeEmbeddingProvider:
    def __init__(
        self,
        *,
        dimensions: int = 4,
        vectors: dict[str, tuple[float, ...]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._profile = EmbeddingProviderProfile(
            provider_id="fake",
            model_id="fake-embedding",
            dimensions=dimensions,
            output_type="dense",
            document_template_version=1,
            endpoint_identity="local-test",
        )
        self._capabilities = EmbeddingProviderCapabilities(
            max_batch_size=20,
            supports_query_document_type=True,
            supports_instruct=True,
            supported_dimensions=(dimensions,),
        )
        self._vectors = vectors or {}
        self._error = error
        self.document_requests = 0
        self.query_requests = 0
        self.closed = False

    @property
    def profile(self) -> EmbeddingProviderProfile:
        return self._profile

    @property
    def capabilities(self) -> EmbeddingProviderCapabilities:
        return self._capabilities

    def _vector(self, text: str) -> EmbeddingVector:
        configured = self._vectors.get(text)
        if configured is None:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            configured = tuple(
                (digest[index % len(digest)] / 127.5) - 1
                for index in range(self.profile.dimensions)
            )
        return EmbeddingVector(values=configured, dimensions=self.profile.dimensions)

    async def embed_documents(self, texts: tuple[str, ...]) -> EmbeddingBatchResult:
        self.document_requests += 1
        if self._error is not None:
            raise self._error
        return EmbeddingBatchResult(
            vectors=tuple(self._vector(text) for text in texts),
            usage=EmbeddingUsage(input_count=len(texts)),
        )

    async def embed_query(self, text: str) -> EmbeddingBatchResult:
        self.query_requests += 1
        if self._error is not None:
            raise self._error
        return EmbeddingBatchResult(
            vectors=(self._vector(text),),
            usage=EmbeddingUsage(input_count=1),
        )

    async def close(self) -> None:
        self.closed = True
