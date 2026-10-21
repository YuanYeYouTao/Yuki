"""Process-local activation hints for inputs arriving from another asyncio task."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from yuki.runtime.activation_tasks import ActivationTasks

if TYPE_CHECKING:
    from yuki.runtime.work_control import WorkControl


class ActiveWorkBindings:
    """Index only live activations; durable leases remain the execution authority.

    An application owns one instance. Binding operations do not await, so another
    task can observe either complete registration on the same event loop. An old
    activation may outlive its lease: its exit must not remove a newer binding.
    """

    def __init__(self, executions: ActivationTasks | None = None) -> None:
        self.executions = executions if executions is not None else ActivationTasks()
        self._controls: dict[str, WorkControl] = {}
        self._completions: dict[asyncio.Future[None], tuple[WorkControl, asyncio.Task[Any]]] = {}

    def get(self, scope_key: str) -> WorkControl | None:
        return self._controls.get(scope_key)

    def completions(self, work_id: str) -> tuple[asyncio.Future[None], ...]:
        """Capture only the original Work's live activation exits."""
        return tuple(
            completed
            for completed, (control, _owner) in self._completions.items()
            if control.current is not None and control.current["id"] == work_id
        )

    def owners(self, work_ids: Collection[str]) -> set[asyncio.Task[Any]]:
        """Keep the exact old activation owner when a scope receives a newer binding."""
        return {
            owner
            for control, owner in self._completions.values()
            if control.current is not None and control.current["id"] in work_ids
        }

    @contextmanager
    def bind(self, scope_key: str, control: WorkControl) -> Iterator[None]:
        with self.executions.track():
            owner = asyncio.current_task()
            assert owner is not None
            completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._completions[completed] = (control, owner)
            self._controls[scope_key] = control
            try:
                yield
            finally:
                if self._controls.get(scope_key) is control:
                    self._controls.pop(scope_key)
                self._completions.pop(completed)
                completed.set_result(None)
