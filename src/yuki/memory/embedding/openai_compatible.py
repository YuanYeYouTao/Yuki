"""OpenAI-compatible dense embeddings; one request per caller batch, no retries."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from yuki.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingProviderCapabilities,
    EmbeddingProviderProfile,
    EmbeddingUsage,
    EmbeddingVector,
)
from yuki.memory.embedding.provider import EmbeddingProviderError
from yuki.memory.embedding.qwen import _endpoint_identity


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        document_template_version: int = 1,
        timeout_seconds: float = 20,
        http_concurrency: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._profile = EmbeddingProviderProfile(
            provider_id="openai_compatible",
            model_id=model,
            dimensions=dimensions,
            document_template_version=document_template_version,
            endpoint_identity=_endpoint_identity(base_url),
        )
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._semaphore = asyncio.Semaphore(http_concurrency)

    @property
    def profile(self) -> EmbeddingProviderProfile:
        return self._profile

    @property
    def capabilities(self) -> EmbeddingProviderCapabilities:
        return EmbeddingProviderCapabilities(
            max_batch_size=2048,
            supports_query_document_type=False,
            supports_instruct=False,
            supported_dimensions=(self.profile.dimensions,),
        )

    async def embed_documents(self, texts: tuple[str, ...]) -> EmbeddingBatchResult:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> EmbeddingBatchResult:
        return await self._embed((text,))

    async def _embed(self, texts: tuple[str, ...]) -> EmbeddingBatchResult:
        async with self._semaphore:
            try:
                response = await self._client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
                    json={
                        "model": self.profile.model_id,
                        "input": list(texts),
                        "dimensions": self.profile.dimensions,
                        "encoding_format": "float",
                    },
                )
            except httpx.TimeoutException:
                raise EmbeddingProviderError(
                    "embedding_timeout", "Embedding provider timed out.", retryable=True
                ) from None
            except httpx.RequestError:
                raise EmbeddingProviderError(
                    "embedding_provider_unavailable",
                    "Embedding provider is unavailable.",
                    retryable=True,
                ) from None
        status = response.status_code
        if status >= 400:
            code = (
                "embedding_authentication_failed"
                if status in {401, 403}
                else "embedding_rate_limited"
                if status == 429
                else "embedding_provider_unavailable"
                if status >= 500
                else "embedding_invalid_request"
            )
            raise EmbeddingProviderError(
                code,
                "Embedding provider rejected the request.",
                retryable=status == 429 or status >= 500,
            )
        try:
            return self._parse(
                response.json(),
                expected_count=len(texts),
                request_id=response.headers.get("x-request-id"),
            )
        except (KeyError, TypeError, ValueError):
            raise EmbeddingProviderError(
                "embedding_invalid_response",
                "Embedding provider returned an invalid response.",
                retryable=False,
            ) from None

    def _parse(
        self, payload: Any, *, expected_count: int, request_id: str | None
    ) -> EmbeddingBatchResult:
        rows = payload["data"]
        if len(rows) != expected_count:
            raise ValueError("embedding response count mismatch")
        ordered = sorted(rows, key=lambda row: row["index"])
        if [row["index"] for row in ordered] != list(range(expected_count)):
            raise ValueError("embedding response indices do not match inputs")
        vectors = tuple(
            EmbeddingVector(values=tuple(row["embedding"]), dimensions=self.profile.dimensions)
            for row in ordered
        )
        usage = payload.get("usage")
        return EmbeddingBatchResult(
            vectors=vectors,
            usage=EmbeddingUsage(
                input_count=expected_count,
                input_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                request_id=request_id,
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(profile={self.profile!r})"
