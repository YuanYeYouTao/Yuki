"""Process-local activation hints for inputs arriving from another asyncio task."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from qq_ai_bot.runtime.activation_tasks import ActivationTasks

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


class ActiveWorkBindings:
    """Index only live activations; durable leases remain the execution authority.

    An application owns one instance. Binding operations do not await, so another
    task can observe either complete registration on the same event loop. An old
    activation may outlive its lease: its exit must not remove a newer binding.
    """

    def __init__(self, executions: ActivationTasks | None = None) -> None:
        self.executions = executions if executions is not None else ActivationTasks()
        self._controls: dict[str, WorkControl] = {}

    def get(self, scope_key: str) -> WorkControl | None:
        return self._controls.get(scope_key)

    def is_active(self, scope_key: str) -> bool:
        control = self.get(scope_key)
        return control is not None and control.current is not None

    @contextmanager
    def bind(self, scope_key: str, control: WorkControl) -> Iterator[None]:
        with self.executions.track():
            self._controls[scope_key] = control
            try:
                yield
            finally:
                if self._controls.get(scope_key) is control:
                    self._controls.pop(scope_key)
