"""Receive Manager events with commit-before-ack semantics."""

import asyncio
import logging
from contextlib import suppress
from typing import Any

from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository


class CompletionReceiver:
    def __init__(
        self, client: SandboxClient, tasks: SandboxTaskRepository, *, poll_seconds: float = 2
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("invalid_completion_poll_interval")
        self.client = client
        self.tasks = tasks
        self._poll_seconds = poll_seconds
        self._worker: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._received = 0

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop(), name="sandbox-completion-receiver")

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def health(self) -> dict[str, Any]:
        return {
            "running": self._worker is not None and not self._worker.done(),
            "last_error_category": self._last_error,
            "acknowledged_this_process": self._received,
        }

    async def _loop(self) -> None:
        delay = self._poll_seconds
        while True:
            try:
                await self.drain_once()
                self._last_error = None
                delay = self._poll_seconds
            except Exception as exc:
                category = type(exc).__name__
                if category != self._last_error:
                    logging.getLogger(__name__).warning(
                        "sandbox_completion_receive_failed category=%s", category
                    )
                self._last_error = category
                delay = min(30, delay * 2)
            await asyncio.sleep(delay)

    async def drain_once(self) -> int:
        page = await self.client.execute("list_code_completions", {}, request_id="completion-read")
        if page.get("error"):
            raise RuntimeError("sandbox_completion_read_failed")
        events = page.get("events")
        if not isinstance(events, list):
            raise ValueError("invalid_completion_page")
        received = 0
        failure: Exception | None = None
        for event in events:
            try:
                await self.tasks.receive(event)
            except Exception as exc:
                # A rejected event remains in Manager. Allow other events in this
                # bounded page to commit; never acknowledge the rejected record.
                failure = exc
                continue
            ack = await self.client.execute(
                "ack_code_completion",
                {"run_id": event["run_id"]},
                request_id="completion-ack",
            )
            if ack.get("acknowledged") is not True or ack.get("run_id") != event["run_id"]:
                raise RuntimeError("sandbox_completion_ack_failed")
            received += 1
            self._received += 1
        if failure is not None:
            raise failure
        return received
