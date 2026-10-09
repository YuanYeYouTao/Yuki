"""Engine limits and the separate host limits that back them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CodeModeLimits:
    # Engine-side limits travel inside dumps; the host re-checks its own.
    max_feed_seconds: float = 10.0
    max_memory_bytes: int = 64 * 1024 * 1024
    max_recursion_depth: int = 200
    # Engine resource cap on suspensions per feed. It is not a business tool
    # budget: every business call is still admitted and charged by the Host.
    max_suspensions: int = 256
    # Host limits, independent of what the engine (or a restored dump) says.
    max_code_bytes: int = 64 * 1024
    max_input_bytes: int = 256 * 1024
    max_output_bytes: int = 64 * 1024
    max_result_bytes: int = 256 * 1024
    max_snapshot_bytes: int = 8 * 1024 * 1024
    max_pending_futures: int = 16
    max_total_suspensions: int = 1024  # Cumulative across restores.
    request_timeout_seconds: float = 30.0  # Parent watchdog; kills the worker.
    max_worker_processes: int = 2
    foreground_reserved_processes: int = 1

    def __post_init__(self) -> None:
        for name in self.__slots__:
            if name == "foreground_reserved_processes":
                continue
            if getattr(self, name) <= 0:
                raise ValueError(f"code_mode_limit_invalid:{name}")
        if not 0 <= self.foreground_reserved_processes < self.max_worker_processes:
            raise ValueError("code_mode_limit_invalid:foreground_reserved_processes")

    @classmethod
    def from_settings(cls, settings: Any) -> CodeModeLimits:
        return cls(
            max_feed_seconds=settings.code_mode_max_feed_seconds,
            max_memory_bytes=settings.code_mode_max_memory_bytes,
            max_output_bytes=settings.code_mode_max_output_bytes,
            max_snapshot_bytes=settings.code_mode_max_snapshot_bytes,
            request_timeout_seconds=settings.code_mode_request_timeout_seconds,
            max_worker_processes=settings.code_mode_max_worker_processes,
            foreground_reserved_processes=settings.code_mode_foreground_reserved_processes,
        )

    def engine(self) -> dict[str, float | int]:
        return {
            "max_feed_duration_secs": self.max_feed_seconds,
            "max_memory": self.max_memory_bytes,
            "max_recursion_depth": self.max_recursion_depth,
            "max_suspensions": self.max_suspensions,
        }

    def accepts_saved_engine_policy(self, saved: object) -> bool:
        # Monty dumps restore their own VM limits. A reduced current limit
        # cannot be applied to that heap through the pinned binding; stop at
        # the original boundary instead of silently restoring a larger budget.
        if not isinstance(saved, dict) or set(saved) != set(self.engine()):
            return False
        return all(
            type(saved[name]) in (int, float) and 0 < saved[name] <= value
            for name, value in self.engine().items()
        )
