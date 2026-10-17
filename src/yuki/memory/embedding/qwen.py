"""DashScope-native Qwen dense embedding provider."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from yuki.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingProviderCapabilities,
    EmbeddingProviderProfile,
    EmbeddingUsage,
    EmbeddingVector,
)
from yuki.memory.embedding.provider import EmbeddingProviderError

_QWEN_CAPABILITIES = EmbeddingProviderCapabilities(
    max_batch_size=20,
    supports_query_document_type=True,
    supports_instruct=True,
    supported_dimensions=(1024,),
)
_EMBEDDING_PATH = "/services/embeddings/text-embedding/text-embedding"


def _endpoint_identity(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MEMORY_EMBEDDING_BASE_URL must be an absolute HTTP(S) URL")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"


class QwenDashScopeEmbeddingProvider:
    """One reusable AsyncClient with strict index and vector validation."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "qwen3.7-text-embedding",
        dimensions: int = 1024,
        output_type: str = "dense",
        document_template_version: int = 1,
        query_instruct: str = (
            "Retrieve personal memory facts relevant to the conversational query."
        ),
        timeout_seconds: float = 20,
        http_concurrency: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if dimensions not in _QWEN_CAPABILITIES.supported_dimensions:
            raise ValueError("unsupported Qwen embedding dimensions")
        if output_type != "dense":
            raise ValueError("MEMORY_EMBEDDING_OUTPUT_TYPE must be dense")
        if not api_key:
            raise ValueError("MEMORY_EMBEDDING_API_KEY is required when embedding is enabled")
        if not model.strip():
            raise ValueError("MEMORY_EMBEDDING_MODEL cannot be empty")
        if not query_instruct.strip():
            raise ValueError("MEMORY_EMBEDDING_QUERY_INSTRUCT cannot be empty")
        if http_concurrency <= 0:
            raise ValueError("MEMORY_EMBEDDING_HTTP_CONCURRENCY must be positive")
        identity = _endpoint_identity(base_url)
        self._url = f"{base_url.rstrip('/')}{_EMBEDDING_PATH}"
        self._api_key = api_key
        self._query_instruct = query_instruct.strip()
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._semaphore = asyncio.Semaphore(http_concurrency)
        self._profile = EmbeddingProviderProfile(
            provider_id="qwen_dashscope",
            model_id=model.strip(),
            dimensions=dimensions,
            output_type=output_type,
            document_template_version=document_template_version,
            endpoint_identity=identity,
        )

    @property
    def profile(self) -> EmbeddingProviderProfile:
        return self._profile

    @property
    def capabilities(self) -> EmbeddingProviderCapabilities:
        return _QWEN_CAPABILITIES

    async def embed_documents(self, texts: tuple[str, ...]) -> EmbeddingBatchResult:
        return await self._embed(texts, text_type="document", instruct=None)

    async def embed_query(self, text: str) -> EmbeddingBatchResult:
        return await self._embed((text,), text_type="query", instruct=self._query_instruct)

    async def _embed(
        self,
        texts: tuple[str, ...],
        *,
        text_type: str,
        instruct: str | None,
    ) -> EmbeddingBatchResult:
        if not texts or any(not text.strip() for text in texts):
            raise EmbeddingProviderError(
                "embedding_invalid_request",
                "Embedding input cannot be empty.",
                retryable=False,
            )
        vectors: list[EmbeddingVector] = []
        input_tokens: int | None = 0
        request_ids: list[str] = []
        size = self.capabilities.max_batch_size
        batches = tuple(texts[offset : offset + size] for offset in range(0, len(texts), size))

        async def request_batch(batch: tuple[str, ...]) -> EmbeddingBatchResult:
            async with self._semaphore:
                return await self._request(batch, text_type=text_type, instruct=instruct)

        results = await asyncio.gather(*(request_batch(batch) for batch in batches))
        for result in results:
            vectors.extend(result.vectors)
            if result.usage.input_tokens is None:
                input_tokens = None
            elif input_tokens is not None:
                input_tokens += result.usage.input_tokens
            if result.usage.request_id:
                request_ids.append(result.usage.request_id)
        return EmbeddingBatchResult(
            vectors=tuple(vectors),
            usage=EmbeddingUsage(
                input_count=len(texts),
                input_tokens=input_tokens,
                request_id=",".join(request_ids) or None,
            ),
        )

    async def _request(
        self,
        texts: tuple[str, ...],
        *,
        text_type: str,
        instruct: str | None,
    ) -> EmbeddingBatchResult:
        parameters: dict[str, Any] = {
            "text_type": text_type,
            "dimension": self.profile.dimensions,
            "output_type": self.profile.output_type,
        }
        if instruct is not None:
            parameters["instruct"] = instruct
        try:
            response = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.profile.model_id,
                    "input": {"texts": list(texts)},
                    "parameters": parameters,
                },
            )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as exc:
            raise EmbeddingProviderError(
                "embedding_timeout", "Embedding provider timed out.", retryable=True
            ) from exc
        except httpx.RequestError as exc:
            raise EmbeddingProviderError(
                "embedding_provider_unavailable",
                "Embedding provider is unavailable.",
                retryable=True,
            ) from exc
        if response.status_code in {401, 403}:
            raise EmbeddingProviderError(
                "embedding_authentication_failed",
                "Embedding provider authentication failed.",
                retryable=False,
            )
        if response.status_code == 429:
            raise EmbeddingProviderError(
                "embedding_rate_limited",
                "Embedding provider rate limit reached.",
                retryable=True,
            )
        if response.status_code >= 500:
            raise EmbeddingProviderError(
                "embedding_provider_unavailable",
                "Embedding provider is unavailable.",
                retryable=True,
            )
        if response.status_code >= 400:
            raise EmbeddingProviderError(
                "embedding_invalid_request",
                "Embedding provider rejected the request.",
                retryable=False,
            )
        try:
            payload = response.json()
            return self._parse(payload, expected_count=len(texts))
        except EmbeddingProviderError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise EmbeddingProviderError(
                "embedding_invalid_response",
                "Embedding provider returned an invalid response.",
                retryable=False,
            ) from exc

    def _parse(self, payload: Any, *, expected_count: int) -> EmbeddingBatchResult:
        if not isinstance(payload, Mapping):
            raise ValueError("response root must be an object")
        output = payload.get("output")
        if not isinstance(output, Mapping):
            raise ValueError("response output must be an object")
        rows = output.get("embeddings")
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise ValueError("embedding response count mismatch")
        ordered: list[EmbeddingVector | None] = [None] * expected_count
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("embedding row must be an object")
            index = row.get("text_index")
            values = row.get("embedding")
            if not isinstance(index, int) or not 0 <= index < expected_count:
                raise ValueError("embedding response index is missing or invalid")
            if ordered[index] is not None:
                raise ValueError("embedding response index is duplicated")
            if not isinstance(values, list):
                raise ValueError("dense embedding is missing")
            ordered[index] = EmbeddingVector(
                values=tuple(float(value) for value in values),
                dimensions=self.profile.dimensions,
            )
        if any(vector is None for vector in ordered):
            raise ValueError("embedding response index is missing")
        usage = payload.get("usage")
        input_tokens: int | None = None
        if isinstance(usage, Mapping):
            token_value = usage.get("input_tokens", usage.get("total_tokens"))
            if isinstance(token_value, int) and token_value >= 0:
                input_tokens = token_value
        request_id = payload.get("request_id")
        return EmbeddingBatchResult(
            vectors=tuple(vector for vector in ordered if vector is not None),
            usage=EmbeddingUsage(
                input_count=expected_count,
                input_tokens=input_tokens,
                request_id=request_id if isinstance(request_id, str) else None,
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_id={self.profile.provider_id!r}, "
            f"model_id={self.profile.model_id!r}, dimensions={self.profile.dimensions!r})"
        )
