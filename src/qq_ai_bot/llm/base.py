"""Provider-neutral LLM interface and sanitized exceptions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse


class LLMError(RuntimeError):
    """Base error safe for categorization but not direct provider details."""

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class LLMTimeoutError(LLMError):
    """The provider did not answer before the configured timeout."""


class LLMUnavailableError(LLMError):
    """The provider is temporarily or permanently unavailable."""


class LLMConfigurationError(LLMError):
    """Required provider configuration is missing."""


class LLMAuthenticationError(LLMError):
    """Provider credentials were rejected."""


class LLMRateLimitError(LLMUnavailableError):
    """The provider rejected the bounded request due to rate limits."""


class LLMInvalidRequestError(LLMError):
    """The provider rejected a request shape or parameter."""


class LLMUnsupportedFeatureError(LLMError):
    """The selected provider does not support a requested feature."""


class LLMInvalidResponseError(LLMError):
    """The provider returned malformed or contradictory data."""


class LLMIncompleteResponseError(LLMError):
    """The bounded recovery request also failed to complete."""


class LLMNativeToolError(LLMError):
    """A provider-native tool produced an unsupported or fatal result."""


class LLMEmptyResponseError(LLMError):
    """The provider returned no usable text."""


class RetryableProviderError(LLMUnavailableError):
    """Internal marker for an explicit HTTP 5xx response."""


class LLMProvider(ABC):
    """Abstract chat-completion provider."""

    @abstractmethod
    async def complete(self, request: ChatRequest) -> ChatResponse:
        """Return one complete, non-streaming response."""

    async def close(self) -> None:
        """Release provider resources."""

        return None
