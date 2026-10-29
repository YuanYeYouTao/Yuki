"""Local processes and byte-cursor logs, owned by the existing Connector service."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .drivers import DriverError, Emit
from .journal import Journal

PAGE_BYTES = 65536
LOG_SEGMENT_BYTES = 4 * 1024 * 1024
LOG_SEGMENTS = 2
logger = logging.getLogger(__name__)


def shell_command(command: str) -> tuple[list[str], str]:
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise DriverError("shell_unavailable", "PowerShell is not installed")
        script = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
            "$OutputEncoding=[Console]::OutputEncoding;" + command
        )
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], shell
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell, "-c", command], shell


def process_arguments(args: dict[str, Any]) -> tuple[list[str], str, Path]:
    argv, shell = shell_command(args["command"])
    return argv, shell, Path(args.get("cwd") or Path.home()).expanduser()


class ProcessGroup(Protocol):
    def cancel(self) -> None: ...

    def close(self) -> None: ...


if sys.platform == "win32":

    class WindowsJob:
        """The OS job owns only this execution's process tree."""

        def __init__(self, pid: int) -> None:
            import ctypes
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.TerminateJobObject.restype = wintypes.BOOL
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.handle = self.kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            try:
                if not process or not self.kernel.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                self.close()
                raise
            finally:
                if process:
                    self.kernel.CloseHandle(process)

        def cancel(self) -> None:
            import ctypes

            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


@dataclass
class LocalRun:
    request_id: str
    run_id: str
    process: asyncio.subprocess.Process
    log: Path
    done: asyncio.Event
    job: ProcessGroup | None = None
    cancelled: bool = False


class LocalTerminal:
    def __init__(self, root: Path, journal: Journal, emit: Emit) -> None:
        self.root, self.journal, self.emit = root, journal, emit
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs: dict[str, LocalRun] = {}
        self.owners: set[asyncio.Task[None]] = set()

    def _original(self, run_id: str) -> tuple[str, dict[str, Any]]:
        row = self.journal.db.execute(
            "SELECT id,command FROM operations WHERE json_extract(command,'$.resource')='terminal' "
            "AND json_extract(command,'$.action')='exec' AND json_extract(command,'$.run_id')=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise DriverError("run_not_found", "The original terminal execution was not found")
        import json

        return row[0], json.loads(row[1])

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, args = command["action"], command.get("arguments", {})
        if action == "exec":
            return await self.start(command)
        run_id = command["run_id"]
        request_id, _ = self._original(run_id)
        run = self.runs.get(run_id)
        if action == "read":
            wait = args.get("wait_seconds", 0)
            if run and wait and not run.done.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(run.done.wait(), wait)
            return self.read(run_id, args.get("cursor", 0))
        if run is None or run.done.is_set():
            return self.read(run_id, 0)
        if action == "input":
            assert run.process.stdin is not None
            data = args["text"].encode("utf-8")
            run.process.stdin.write(data)
            await run.process.stdin.drain()
            return {
                "run_id": run_id,
                "operation_id": command["id"],
                "status": "running",
                "pending": True,
                "input_delivered": True,
            }
        if action == "interrupt":
            if sys.platform == "win32":
                raise DriverError(
                    "interrupt_unsupported",
                    "This Windows pipe terminal has no console for Ctrl-Break; "
                    "cancel stops its original process tree",
                )
            else:
                os.killpg(run.process.pid, signal.SIGINT)
        elif action in {"cancel", "close"}:
            run.cancelled = True
            self._cancel(run)
            await run.done.wait()
        else:
            raise DriverError("invalid_terminal_action", f"Unsupported terminal action: {action}")
        return {**self.read(run_id, 0), "request_id": request_id}

    async def start(self, command: dict[str, Any]) -> dict[str, Any]:
        args, run_id = command["arguments"], command["run_id"]
        if args.get("tty", False):
            raise DriverError(
                "tty_unsupported", "This device supports pipe terminals, not a true TTY"
            )
        argv, shell, directory = await asyncio.to_thread(process_arguments, args)
        process_options: dict[str, Any]
        if sys.platform == "win32":
            process_options = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            }
        else:
            process_options = {"start_new_session": True}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=directory,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                **process_options,
            )
        except OSError as error:
            raise DriverError("shell_start_failed", str(error)) from error
        run = LocalRun(
            command["id"], run_id, process, self.root / f"{run_id}.0.log", asyncio.Event()
        )
        try:
            if sys.platform == "win32":
                run.job = WindowsJob(process.pid)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        self.runs[run_id] = run
        task = asyncio.create_task(self._watch(run, args.get("timeout_seconds")))
        self.owners.add(task)

        def finished(task: asyncio.Task[None]) -> None:
            self.owners.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.error(
                    "Terminal observation failed: %s (%s)", run.run_id, type(error).__name__
                )
                if self.journal.operation(run.request_id).get("status") not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    self.journal.annotate(
                        run.request_id,
                        {
                            "status": "unknown",
                            "pending": True,
                            "reason": "terminal_observation_lost",
                            "error_type": type(error).__name__,
                        },
                    )

        task.add_done_callback(finished)
        return {
            "run_id": run_id,
            "request_id": command["id"],
            "status": "running",
            "pending": True,
            "admitted": True,
            "shell": shell,
            "cwd": str(directory),
        }

    async def _watch(self, run: LocalRun, timeout_seconds: float | None) -> None:
        async def output() -> None:
            assert run.process.stdout is not None
            offset = 0
            stream = run.log.open("xb", buffering=0)
            try:
                while chunk := await run.process.stdout.read(PAGE_BYTES):
                    while chunk:
                        if stream.tell() == LOG_SEGMENT_BYTES:
                            stream.close()
                            run.log = self.root / f"{run.run_id}.{offset}.log"
                            stream = run.log.open("xb", buffering=0)
                            for _, expired in self._log_segments(run.run_id)[:-LOG_SEGMENTS]:
                                expired.unlink()
                        selected = chunk[: LOG_SEGMENT_BYTES - stream.tell()]
                        stream.write(selected)
                        offset += len(selected)
                        chunk = chunk[len(selected) :]
            finally:
                stream.close()

        pump = asyncio.create_task(output())
        timed_out = False
        try:
            if timeout_seconds:
                try:
                    await asyncio.wait_for(asyncio.shield(run.process.wait()), timeout_seconds)
                except TimeoutError:
                    timed_out = True
                    self._cancel(run)
                    await run.process.wait()
            else:
                await run.process.wait()
            await pump
            result = {
                "run_id": run.run_id,
                "status": "cancelled"
                if run.cancelled
                else "failed"
                if run.process.returncode
                else "succeeded",
                "pending": False,
                "exit_code": run.process.returncode,
                **({"timed_out": True} if timed_out else {}),
            }
            await self.emit(
                "completed",
                {
                    "type": "terminal_event",
                    "request_id": run.request_id,
                    "operation_id": run.request_id,
                    "run_id": run.run_id,
                    "result": result,
                },
            )
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            if run.job:
                run.job.close()
            run.done.set()

    def _log_segments(self, run_id: str) -> list[tuple[int, Path]]:
        # Segment filenames retain original absolute byte positions across an
        # owner restart. No per-output journal writes or second index are needed.
        segments = [
            (int(path.name[len(run_id) + 1 : -4]), path)
            for path in self.root.glob(f"{run_id}.*.log")
        ]
        legacy = self.root / f"{run_id}.log"
        if legacy.is_file():
            segments.append((0, legacy))
        return sorted(segments)

    def read(self, run_id: str, cursor: int) -> dict[str, Any]:
        request_id, _ = self._original(run_id)
        receipt = self.journal.operation(request_id)
        segments = self._log_segments(run_id)
        offset = max(cursor, segments[0][0]) if segments else cursor
        position = offset
        chunk = b""
        end = 0
        output_lost = offset > cursor
        for start, path in segments:
            with path.open("rb") as stream:
                end = start + os.fstat(stream.fileno()).st_size
                if position >= end or len(chunk) == PAGE_BYTES:
                    continue
                if position < start:
                    output_lost = True
                    position = start
                stream.seek(position - start)
                selected = stream.read(PAGE_BYTES - len(chunk))
                chunk += selected
                position += len(selected)
        eof = position >= end
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        terminal = receipt.get("status") in {"succeeded", "failed", "cancelled"}
        text = decoder.decode(chunk, final=eof and terminal)
        remainder = decoder.getstate()[0]
        next_cursor = position - len(remainder)
        run = self.runs.get(run_id)
        if run is None and receipt.get("pending"):
            receipt = {
                **receipt,
                "status": "unknown",
                "pending": True,
                "reason": "process_owner_lost",
            }
        return {
            **receipt,
            "run_id": run_id,
            "request_id": request_id,
            "output": text,
            "output_offset": offset,
            "output_lost": output_lost,
            "next_cursor": next_cursor,
            "eof": eof,
        }

    @staticmethod
    def _cancel(run: LocalRun) -> None:
        if run.job:
            run.job.cancel()
        elif sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(run.process.pid, signal.SIGKILL)
        else:
            run.process.kill()

    async def close(self) -> None:
        for run in self.runs.values():
            if not run.done.is_set():
                run.cancelled = True
                self._cancel(run)
        await asyncio.gather(*self.owners)
