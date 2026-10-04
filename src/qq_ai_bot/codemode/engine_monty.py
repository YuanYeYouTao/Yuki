"""Pinned Monty native worker driven by manual suspend/resume.

The pinned `pydantic-monty-client` binding spawns `monty subprocess` workers
through monty-pool (env_clear, piped stdio, kill_on_drop). This module uses only
`feed_start`, manual `resume`, `dump` and `load_snapshot`: never `resume_auto`,
never `feed_run`/external_lookup callbacks, mounts, `os=` handlers, remote
transports or host `exec`. Every suspension is returned to the Host, which alone
decides whether a business call may run.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qq_ai_bot.codemode.driver_types import (
    EngineAnswer,
    EngineCall,
    EngineFailure,
    EngineOutcome,
    FailureCategory,
    HostCounters,
    JsonValue,
)
from qq_ai_bot.codemode.limits import CodeModeLimits
from qq_ai_bot.codemode.snapshot_binding import matches_saved

PINNED_MONTY_SHA = "3f9d6ef413fb951e5b80113b7088d535bd028fcb"
PINNED_BINDING_VERSION = "1.0.1"
SUPPORTED_PLATFORMS = frozenset({("Darwin", "arm64"), ("Linux", "x86_64"), ("Linux", "aarch64")})
# Engine call names the manifest can never bind, independent of the manifest.
_RESERVED_NAMES = frozenset({"__import__", "open", "exec", "eval", "compile", "input"})


class CodeEngineUnavailable(RuntimeError):
    """The pinned worker cannot be used; the Host rejects Code Mode admission."""


def _load_binding() -> Any:
    try:
        import pydantic_monty
    except ImportError as exc:  # The Host never falls back to another executor.
        raise CodeEngineUnavailable("code_engine_binding_missing") from exc
    if getattr(pydantic_monty, "__version__", None) != PINNED_BINDING_VERSION:
        raise CodeEngineUnavailable("code_engine_binding_version_mismatch")
    return pydantic_monty


@dataclass(frozen=True, slots=True)
class PinnedWorker:
    """An explicit, digest-verified native worker; no PATH or wheel discovery."""

    binary_path: Path
    sha256: str

    @classmethod
    def from_settings(cls, settings: Any) -> PinnedWorker:
        if settings.code_mode_worker_path is None or not settings.code_mode_worker_sha256:
            raise CodeEngineUnavailable("code_engine_not_configured")
        return cls(Path(settings.code_mode_worker_path), settings.code_mode_worker_sha256)

    def verify(self) -> str:
        if (platform.system(), platform.machine()) not in SUPPORTED_PLATFORMS:
            raise CodeEngineUnavailable("code_engine_platform_unsupported")
        path = self.binary_path
        if not path.is_absolute() or not path.is_file():
            raise CodeEngineUnavailable("code_engine_binary_missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self.sha256.lower():
            raise CodeEngineUnavailable("code_engine_binary_digest_mismatch")
        return digest


def strict_json(value: Any, *, limit: int) -> JsonValue:
    """Accept only plain JSON data; tuples become lists. Everything else is refused."""

    def walk(item: Any, depth: int) -> JsonValue:
        if depth > 64:
            raise ValueError("code_value_too_deep")
        if item is None or isinstance(item, bool | str):
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("code_value_not_json")
            return item
        if isinstance(item, list | tuple):
            return [walk(x, depth + 1) for x in item]
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise ValueError("code_value_not_json")
            return {k: walk(v, depth + 1) for k, v in item.items()}
        raise ValueError("code_value_not_json")

    checked = walk(value, 0)
    if len(json.dumps(checked, ensure_ascii=False, allow_nan=False).encode()) > limit:
        raise ValueError("code_value_too_large")
    return checked


class _OutputSink:
    """Bounded capture; the engine feed limit stops a flood, the host drops excess."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False
        self.received = 0

    def __call__(self, _stream: str, text: str) -> None:
        encoded = len(text.encode())
        self.received += encoded
        if self.truncated:
            return
        if self.size + encoded > self.limit:
            self.truncated = True
            return
        self.parts.append(text)
        self.size += encoded

    def take(self) -> tuple[str, bool, int]:
        text, truncated, received = "".join(self.parts), self.truncated, self.received
        self.parts, self.size, self.truncated, self.received = [], 0, False, 0
        return text, truncated, received


class MontyEngine:
    """Owns the worker pool resource only: no actor, prompt, or tool results."""

    def __init__(self, worker: PinnedWorker, limits: CodeModeLimits) -> None:
        self.worker = worker
        self.limits = limits
        self.binding = _load_binding()
        self._pool: Any = None

    async def __aenter__(self) -> MontyEngine:
        self.worker.verify()
        self._pool = self.binding.AsyncMonty(
            binary_path=self.worker.binary_path,
            min_processes=0,
            # A worker never serves a second script: no state or handle reuse.
            max_checkouts_per_worker=1,
            request_timeout=self.limits.request_timeout_seconds,
        )
        await self._pool.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.__aexit__(None, None, None)

    def run(self, manifest: frozenset[str]) -> MontyRun:
        if self._pool is None:
            raise CodeEngineUnavailable("code_engine_not_started")
        if manifest & _RESERVED_NAMES:
            raise ValueError("code_manifest_reserved_name")
        return MontyRun(self, manifest)


class MontyRun:
    """One script on one isolated worker. Answers are the Host's, call by call."""

    def __init__(self, engine: MontyEngine, manifest: frozenset[str]) -> None:
        self.engine = engine
        self.manifest = manifest
        self.limits = engine.limits
        self.counters = HostCounters()
        self._session_cm: Any = None
        self._session: Any = None
        self._snapshot: Any = None
        self._call: EngineCall | None = None
        self._output = _OutputSink(self.limits.max_output_bytes)
        self._closed = False
        # Engine calls answered with a future and not yet settled.
        self._futures: set[int] = set()

    # -- lifecycle -----------------------------------------------------------------

    async def _open(self) -> None:
        if self._closed or self._session is not None:
            raise RuntimeError("code_run_state_invalid")
        self._session_cm = self.engine._pool.checkout(
            limits=self.limits.engine(),
            # Deterministic, host-free clock/sleep/random; no host callbacks.
            os_policy={"sleep": "zero", "process_time": "zero"},
        )
        self._session = await self._session_cm.__aenter__()

    async def terminate(self) -> None:
        """Discard the worker. Idempotent; never leaves a callback or forked VM."""
        self._closed = True
        self._snapshot, self._call = None, None
        session_cm, self._session_cm, self._session = self._session_cm, None, None
        if session_cm is not None:
            try:
                await session_cm.__aexit__(None, None, None)
            except Exception:  # A dead worker has nothing to release.
                pass

    # -- execution -----------------------------------------------------------------

    async def start(self, code: str, inputs: dict[str, JsonValue]) -> EngineOutcome:
        if len(code.encode()) > self.limits.max_code_bytes:
            return self._fail("limit_code", "code_too_large", discard=False)
        try:
            checked = strict_json(inputs, limit=self.limits.max_input_bytes)
        except ValueError as exc:
            return self._fail("result_not_json", str(exc), discard=False)
        if not isinstance(checked, dict):
            return self._fail("result_not_json", "code_inputs_not_object", discard=False)
        await self._open()
        self.counters.feeds += 1
        return await self._advance(
            lambda: self._session.feed_start(
                code, inputs=checked, print_callback=self._output, skip_type_check=True
            )
        )

    async def answer(self, engine_call_id: int, answer: EngineAnswer) -> EngineOutcome:
        call = self._call
        if call is None or call.kind != "function" or call.engine_call_id != engine_call_id:
            raise RuntimeError("code_answer_not_pending")
        if call.function_name not in self.manifest:
            raise RuntimeError("code_answer_outside_manifest")
        payload = self._answer_payload(answer)
        if answer.kind == "future":
            self._futures.add(engine_call_id)
        snapshot = self._take()
        return await self._advance(lambda: snapshot.resume(payload))

    async def settle(self, results: dict[int, EngineAnswer]) -> EngineOutcome:
        call = self._call
        if call is None or call.kind != "future":
            raise RuntimeError("code_settle_not_pending")
        if not results or not set(results) <= set(call.pending_call_ids):
            raise RuntimeError("code_settle_unknown_call")
        if any(answer.kind == "future" for answer in results.values()):
            raise RuntimeError("code_settle_requires_result")
        payload = {key: self._answer_payload(value) for key, value in results.items()}
        self._futures.difference_update(results)
        snapshot = self._take()
        return await self._advance(lambda: snapshot.resume(payload))

    def dump(self) -> bytes:
        """The live suspension only; it is persisted by the Host before any answer."""
        if self._snapshot is None:
            raise RuntimeError("code_dump_not_suspended")
        data: bytes = self._snapshot.dump()
        if len(data) > self.limits.max_snapshot_bytes:
            raise ValueError("code_snapshot_capacity")
        return data

    async def restore(
        self, dump: bytes, saved: dict[str, Any], counters: HostCounters
    ) -> EngineOutcome:
        """Load a Host-verified dump; it must re-announce exactly the saved boundary."""
        if len(dump) > self.limits.max_snapshot_bytes:
            return self._fail("limit_snapshot", "code_snapshot_capacity", discard=False)
        self.counters = counters
        await self._open()
        outcome = await self._advance(lambda: self._session.load_snapshot(dump), restoring=True)
        if outcome.status != "suspended" or outcome.call is None:
            if outcome.status == "failed":
                return outcome
            return await self._reject_restore()
        announced = outcome.call
        if not matches_saved(saved, announced):
            return await self._reject_restore()
        return outcome

    # -- internals -----------------------------------------------------------------

    async def _reject_restore(self) -> EngineOutcome:
        await self.terminate()
        return EngineOutcome(
            "failed",
            failure=EngineFailure(
                "snapshot_binding_conflict", "snapshot_binding_conflict", worker_discarded=True
            ),
        )

    def _take(self) -> Any:
        snapshot, self._snapshot, self._call = self._snapshot, None, None
        if snapshot is None:
            raise RuntimeError("code_run_state_invalid")
        return snapshot

    def _answer_payload(self, answer: EngineAnswer) -> dict[str, Any]:
        if answer.kind == "future":
            if len(self._futures) >= self.limits.max_pending_futures:
                raise RuntimeError("code_wait_queue_full")
            return {"future": ...}
        if answer.kind == "error":
            return {"exc_type": answer.error_type, "message": answer.message[:2000]}
        return {"return_value": strict_json(answer.value, limit=self.limits.max_result_bytes)}

    async def _advance(self, step: Callable[[], Any], *, restoring: bool = False) -> EngineOutcome:
        """Drive until the next business suspension, completion or failure.

        OS requests and undefined names never reach business code: they are
        refused inside the sandbox here, and only the closed manifest is
        surfaced to the Host.
        """
        binding = self.engine.binding
        try:
            snapshot = await step()
            while True:
                if isinstance(snapshot, binding.MontyComplete):
                    return self._completed(snapshot.output)
                self.counters.suspensions += 1
                if self.counters.suspensions > self.limits.max_total_suspensions:
                    return await self._fail_async(
                        "limit_suspensions", "code_total_suspensions_exceeded"
                    )
                if isinstance(snapshot, binding.AsyncNameLookupSnapshot):
                    # Name probing finds nothing: every lookup raises NameError.
                    self.counters.denied_calls += 1
                    snapshot = await snapshot.resume()
                    continue
                if isinstance(snapshot, binding.AsyncFutureSnapshot):
                    pending = tuple(sorted(snapshot.pending_call_ids))
                    # After restore the engine is the source of truth for open futures.
                    self._futures = set(pending)
                    if len(pending) > self.limits.max_pending_futures:
                        return await self._fail_async("limit_wait_queue", "code_wait_queue_full")
                    return self._suspend(
                        snapshot,
                        EngineCall(
                            "future",
                            self.counters.feeds - 1,
                            None,
                            None,
                            pending_call_ids=pending,
                        ),
                    )
                # A function snapshot: OS call, reserved/unknown name, or manifest call.
                if snapshot.is_os_function:
                    self.counters.denied_calls += 1
                    snapshot = await snapshot.resume_not_handled()
                    continue
                name = str(snapshot.function_name)
                if snapshot.object_id is not None or name not in self.manifest:
                    self.counters.denied_calls += 1
                    snapshot = await snapshot.resume(
                        {"exc_type": "NameError", "message": f"name '{name}' is not defined"}
                    )
                    continue
                try:
                    args = strict_json(list(snapshot.args), limit=self.limits.max_input_bytes)
                    kwargs = strict_json(dict(snapshot.kwargs), limit=self.limits.max_input_bytes)
                except ValueError as exc:
                    self.counters.denied_calls += 1
                    snapshot = await snapshot.resume(
                        {"exc_type": "TypeError", "message": f"{name}: {exc}"}
                    )
                    continue
                assert isinstance(args, list) and isinstance(kwargs, dict)
                return self._suspend(
                    snapshot,
                    EngineCall(
                        "function",
                        self.counters.feeds - 1,
                        int(snapshot.call_id),
                        name,
                        tuple(args),
                        kwargs,
                    ),
                )
        except binding.MontySyntaxError as exc:
            return await self._fail_async("syntax", _public_message(exc))
        except binding.MontyCrashedError as exc:
            category: FailureCategory = "limit_time" if exc.timed_out else "crashed"
            return await self._fail_async(category, "code_worker_crashed")
        except binding.MontyRuntimeError as exc:
            inner = exc.exception()
            kind = type(inner).__name__
            text = str(inner)
            if kind == "TimeoutError" and "time limit exceeded" in text:
                return await self._fail_async("limit_time", text)
            if kind == "MemoryError" and "memory limit exceeded" in text:
                return await self._fail_async("limit_memory", text)
            if kind == "RuntimeError" and "suspension limit" in text:
                return await self._fail_async("limit_suspensions", text)
            if restoring and "protocol violation" in text:
                # Rejected dump (old format, truncated, corrupt): the worker is discarded.
                return await self._fail_async("snapshot_binding_conflict", text)
            return await self._fail_async("runtime", f"{kind}: {text}"[:2000])
        except binding.MontyError as exc:
            return await self._fail_async("protocol", type(exc).__name__)

    def _suspend(self, snapshot: Any, call: EngineCall) -> EngineOutcome:
        self._snapshot, self._call = snapshot, call
        text, truncated, received = self._output.take()
        self.counters.output_bytes += received
        return EngineOutcome("suspended", call=call, stdout=text, stdout_truncated=truncated)

    def _completed(self, output: Any) -> EngineOutcome:
        text, truncated, received = self._output.take()
        self.counters.output_bytes += received
        try:
            value = strict_json(output, limit=self.limits.max_result_bytes)
        except ValueError as exc:
            return EngineOutcome(
                "failed",
                failure=EngineFailure("result_not_json", str(exc), worker_discarded=False),
                stdout=text,
                stdout_truncated=truncated,
            )
        return EngineOutcome("completed", output=value, stdout=text, stdout_truncated=truncated)

    def _fail(self, category: FailureCategory, message: str, *, discard: bool) -> EngineOutcome:
        return EngineOutcome(
            "failed", failure=EngineFailure(category, message, worker_discarded=discard)
        )

    async def _fail_async(self, category: FailureCategory, message: str) -> EngineOutcome:
        text, truncated, received = self._output.take()
        self.counters.output_bytes += received
        # Any failure ends this script: a possibly-damaged heap is never reused.
        await self.terminate()
        return EngineOutcome(
            "failed",
            failure=EngineFailure(category, message, worker_discarded=True),
            stdout=text,
            stdout_truncated=truncated,
        )


def _public_message(exc: Exception) -> str:
    inner = exc.exception() if hasattr(exc, "exception") else exc
    return f"{type(inner).__name__}: {inner}"[:2000]
