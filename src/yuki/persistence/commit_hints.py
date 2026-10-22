"""Disposable post-commit wakeups; no facts, retries, or durable event log."""

import asyncio


class CommitHints:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[bool]] = set()

    def subscribe(self) -> asyncio.Queue[bool]:
        queue: asyncio.Queue[bool] = asyncio.Queue(64)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bool]) -> None:
        self.subscribers.discard(queue)

    def publish(self) -> None:
        for queue in tuple(self.subscribers):
            if queue.full():
                # A lost final hint must disconnect even if no later commit occurs.
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(False)
            else:
                queue.put_nowait(True)
