"""Vision provider protocol and sanitized errors."""

from __future__ import annotations

from typing import Protocol

from yuki.vision.models import (
    PreparedVisualInput,
    VisionAnalysisOptions,
    VisualObservation,
)


class VisionError(RuntimeError):
    """A sanitized provider error safe for categorization."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class VisionConfigurationError(VisionError):
    """The vision provider is not configured."""


class VisionProvider(Protocol):
    """Provider-neutral, non-streaming visual analysis contract."""

    @property
    def provider_name(self) -> str:
        """Return the stable provider identifier used in cache keys."""

    @property
    def model_name(self) -> str:
        """Return the configured model identifier used in cache keys."""

    async def analyze(
        self,
        inputs: tuple[PreparedVisualInput, ...],
        question: str,
        *,
        options: VisionAnalysisOptions | None = None,
    ) -> VisualObservation:
        """Analyze all selected images in one provider request."""

    async def close(self) -> None:
        """Release provider-owned resources."""
