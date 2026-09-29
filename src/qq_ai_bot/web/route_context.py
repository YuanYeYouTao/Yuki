"""Bind local web-tool execution to the invoking model task."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from qq_ai_bot.model_runtime.models import ModelTask

current_web_model_task: ContextVar[ModelTask | None] = ContextVar(
    "current_web_model_task", default=None
)


@contextmanager
def web_model_task(task: ModelTask) -> Iterator[None]:
    token = current_web_model_task.set(task)
    try:
        yield
    finally:
        current_web_model_task.reset(token)
