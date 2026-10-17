"""Deterministic visual provider for tests and offline development."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from yuki.vision.models import (
    PreparedVisualInput,
    VisionAnalysisOptions,
    VisualItemObservation,
    VisualObservation,
)

FakeVisionResponder = Callable[
    [tuple[PreparedVisualInput, ...], str],
    VisualObservation,
]


class FakeVisionProvider:
    """Return deterministic observations without network access."""

    def __init__(
        self,
        responder: FakeVisionResponder | None = None,
        *,
        delay_seconds: float = 0,
        model: str = "fake-vision",
    ) -> None:
        self._responder = responder
        self._delay_seconds = max(0.0, delay_seconds)
        self._model = model
        self.requests: list[tuple[tuple[PreparedVisualInput, ...], str]] = []
        self.request_options: list[VisionAnalysisOptions] = []
        self.closed = False

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return self._model

    async def analyze(
        self,
        inputs: tuple[PreparedVisualInput, ...],
        question: str,
        *,
        options: VisionAnalysisOptions | None = None,
    ) -> VisualObservation:
        self.requests.append((inputs, question))
        self.request_options.append(options or VisionAnalysisOptions())
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._responder is not None:
            return self._responder(inputs, question)
        return VisualObservation(
            items=tuple(
                VisualItemObservation(
                    index=index,
                    description=f"测试图片 {index}",
                    confidence=1.0,
                )
                for index, _input in enumerate(inputs, start=1)
            ),
            overall_description="测试视觉观察",
            partial_failure=False,
            provider="fake",
            model=self._model,
            latency_seconds=self._delay_seconds,
        )

    async def close(self) -> None:
        self.closed = True
