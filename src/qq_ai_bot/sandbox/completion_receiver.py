"""Receive Manager events with commit-before-ack semantics."""

from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository


class CompletionReceiver:
    def __init__(self, client: SandboxClient, tasks: SandboxTaskRepository) -> None:
        self.client = client
        self.tasks = tasks

    async def drain_once(self) -> int:
        page = await self.client.execute("list_code_completions", {}, request_id="completion-read")
        if page.get("error"):
            raise RuntimeError("sandbox_completion_read_failed")
        events = page.get("events")
        if not isinstance(events, list):
            raise ValueError("invalid_completion_page")
        received = 0
        for event in events:
            await self.tasks.receive(event)
            ack = await self.client.execute(
                "ack_code_completion",
                {"run_id": event["run_id"]},
                request_id="completion-ack",
            )
            if ack.get("acknowledged") is not True or ack.get("run_id") != event["run_id"]:
                raise RuntimeError("sandbox_completion_ack_failed")
            received += 1
        return received
