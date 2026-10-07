"""Host-facing Code Mode driver types; no engine object crosses this boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

JsonValue = Any  # Validated by the driver as strict JSON (dict/list/str/int/float/bool/None).

FailureCategory = Literal[
    "syntax",  # The script did not compile.
    "runtime",  # An uncaught sandbox exception; the script's own error.
    "limit_time",  # Engine feed limit or host watchdog; the worker is discarded.
    "limit_memory",
    "limit_suspensions",  # Engine resource cap, never a business tool budget.
    "limit_output",
    "limit_code",
    "limit_wait_queue",
    "limit_snapshot",
    "crashed",  # The native worker died; the host process is unaffected.
    "result_not_json",
    "snapshot_binding_conflict",
    "protocol",
]

# Exception types a host answer may raise inside the sandbox. A closed set keeps
# host tracebacks and private exception classes out of the VM.
ANSWER_ERROR_TYPES = frozenset(
    {"RuntimeError", "PermissionError", "ValueError", "TimeoutError", "LookupError"}
)


@dataclass(frozen=True, slots=True)
class EngineCall:
    """One suspension the host must answer; ids are the engine's, not business ids."""

    kind: Literal["function", "future"]
    feed_index: int
    engine_call_id: int | None  # Function call; None for a future wait.
    function_name: str | None
    args: tuple[JsonValue, ...] = ()
    kwargs: dict[str, JsonValue] = field(default_factory=dict)
    pending_call_ids: tuple[int, ...] = ()  # Future wait: engine calls still open.


@dataclass(frozen=True, slots=True)
class EngineAnswer:
    """Host result for one engine call. Only JSON values or a closed error type."""

    kind: Literal["value", "error", "future"]
    value: JsonValue = None
    error_type: str = "RuntimeError"
    message: str = ""

    @classmethod
    def ok(cls, value: JsonValue) -> EngineAnswer:
        return cls("value", value=value)

    @classmethod
    def error(cls, message: str, error_type: str = "RuntimeError") -> EngineAnswer:
        if error_type not in ANSWER_ERROR_TYPES:
            raise ValueError("code_answer_error_type_not_allowed")
        return cls("error", error_type=error_type, message=message)

    @classmethod
    def future(cls) -> EngineAnswer:
        return cls("future")


@dataclass(frozen=True, slots=True)
class EngineFailure:
    category: FailureCategory
    message: str
    worker_discarded: bool


@dataclass(frozen=True, slots=True)
class EngineOutcome:
    """Exactly one of completed/suspended/failed is meaningful."""

    status: Literal["completed", "suspended", "failed"]
    output: JsonValue = None
    call: EngineCall | None = None
    failure: EngineFailure | None = None
    stdout: str = ""
    stdout_truncated: bool = False


@dataclass(slots=True)
class HostCounters:
    """Cumulative across restores; persisted with each boundary, never reset."""

    suspensions: int = 0
    denied_calls: int = 0
    output_bytes: int = 0
    feeds: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "suspensions": self.suspensions,
            "denied_calls": self.denied_calls,
            "output_bytes": self.output_bytes,
            "feeds": self.feeds,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HostCounters:
        return cls(**{key: int(value.get(key, 0)) for key in cls.__slots__})


class ScriptRun(Protocol):
    """A single script execution on one isolated worker."""

    counters: HostCounters

    async def start(self, code: str, inputs: dict[str, JsonValue]) -> EngineOutcome: ...

    async def answer(self, engine_call_id: int, answer: EngineAnswer) -> EngineOutcome: ...

    async def settle(self, results: dict[int, EngineAnswer]) -> EngineOutcome: ...

    def dump(self) -> bytes: ...

    async def restore(
        self, dump: bytes, saved: dict[str, Any], counters: HostCounters
    ) -> EngineOutcome: ...

    async def terminate(self) -> None: ...
