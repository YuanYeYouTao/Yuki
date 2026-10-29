"""Versioned file streams over the original device RPC; bytes never enter tool text."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import threading
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import Any

from .drivers import DriverError
from .journal import Journal

CHUNK_BYTES = 65536


def file_version(stat: os.stat_result) -> str:
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


@dataclass
class FileSource:
    stream: BufferedReader
    metadata: dict[str, Any]
    requested_path: str
    lock: threading.Lock
    observed_version: str


class FileTransfers:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.sources: dict[str, asyncio.Task[FileSource]] = {}
        self.writers: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _open(requested_path: str, transfer_id: str) -> FileSource:
        path = Path(requested_path).expanduser()
        try:
            stream = path.open("rb")
        except OSError as error:
            raise DriverError("file_open_failed", str(error)) from error
        try:
            version = file_version(os.fstat(stream.fileno()))
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            stat = os.fstat(stream.fileno())
            if file_version(stat) != version:
                raise DriverError("file_changed", "The selected file changed while it was read")
            stream.seek(0)
            return FileSource(
                stream,
                {
                    "transfer_id": transfer_id,
                    "path": str(path),
                    "name": path.name,
                    "version": version,
                    "size": stat.st_size,
                    "sha256": digest,
                    "chunk_bytes": CHUNK_BYTES,
                },
                requested_path,
                threading.Lock(),
                version,
            )
        except BaseException:
            stream.close()
            raise

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, args = command["action"], command.get("arguments", {})
        if action == "get":
            row = self.journal.db.execute(
                "SELECT command FROM operations WHERE id=?", (args["transfer_id"],)
            ).fetchone()
            if row is not None:
                import json

                original = json.loads(row[0])
                if (
                    original.get("resource") != "file"
                    or original["action"] != "upload"
                    or original["arguments"]["path"] != args["path"]
                ):
                    raise DriverError(
                        "transfer_target_mismatch", "The original transfer has another path"
                    )
            result = self.journal.operation(args["transfer_id"])
            return result
        if action == "stat":
            transfer_id = command["id"]
            if transfer_id not in self.sources:
                self.sources[transfer_id] = asyncio.create_task(
                    asyncio.to_thread(self._open, args["path"], transfer_id)
                )
            source = await asyncio.shield(self.sources[transfer_id])
            if args["path"] != source.requested_path:
                raise DriverError(
                    "transfer_target_mismatch", "The original transfer has another path"
                )
            return source.metadata
        if action in {"read", "release"}:
            transfer_id = args["transfer_id"]
            if transfer_id not in self.sources:
                raise DriverError("transfer_unavailable", "The original file stream is unavailable")
            source = await asyncio.shield(self.sources[transfer_id])
            if action == "release":
                await asyncio.to_thread(self._close_source, source)
                self.sources.pop(transfer_id)
                return {"transfer_id": transfer_id, "released": True}
            return await asyncio.to_thread(self._read_locked, source, args)
        if action == "write":
            transfer_id = args["transfer_id"]
            original = {
                "id": transfer_id,
                "resource": "file",
                "action": "upload",
                "arguments": {"path": args["path"], "size": args["size"], "sha256": args["sha256"]},
            }
            self.journal.begin(original)
            async with self.writers.setdefault(transfer_id, asyncio.Lock()):
                completed = self.journal.operation(transfer_id)
                if completed.get("status") == "succeeded":
                    return completed
                result = await asyncio.to_thread(self._write, command)
                if result.get("status") == "succeeded":
                    self.journal.complete(transfer_id, {"result": result})
                else:
                    self.journal.annotate(transfer_id, {"path": args["path"], "pending": True})
                return result
        raise DriverError("invalid_file_action", f"Unsupported file action: {action}")

    @staticmethod
    def _close_source(source: FileSource) -> None:
        with source.lock:
            source.stream.close()

    @staticmethod
    def _read_locked(source: FileSource, args: dict[str, Any]) -> dict[str, Any]:
        with source.lock:
            return FileTransfers._read(source, args)

    @staticmethod
    def _read(source: FileSource, args: dict[str, Any]) -> dict[str, Any]:
        version = source.metadata["version"]
        if args["version"] != version:
            raise DriverError("file_changed", "The original file version is no longer available")
        FileTransfers._validate_source(source)
        offset = args.get("offset", 0)
        source.stream.seek(offset)
        data = source.stream.read(CHUNK_BYTES)
        FileTransfers._validate_source(source)
        return {
            **source.metadata,
            "offset": offset,
            "next_offset": offset + len(data),
            "eof": offset + len(data) == source.metadata["size"],
            "data_base64": base64.b64encode(data).decode("ascii"),
        }

    @staticmethod
    def _validate_source(source: FileSource) -> None:
        current = file_version(os.fstat(source.stream.fileno()))
        if current == source.observed_version:
            return
        # Unlink/rename can change ctime of the retained original inode. Prove
        # its selected bytes remain the same instead of opening today's path.
        source.stream.seek(0)
        digest = hashlib.file_digest(source.stream, "sha256").hexdigest()
        if (
            digest != source.metadata["sha256"]
            or file_version(os.fstat(source.stream.fileno())) != current
        ):
            raise DriverError("file_changed", "The selected file changed during transfer")
        source.observed_version = current

    @staticmethod
    def _write(command: dict[str, Any]) -> dict[str, Any]:
        args = command["arguments"]
        destination = Path(args["path"]).expanduser()
        transfer_id = args["transfer_id"]
        # The stable transfer name belongs to this original write, not to the user file.
        suffix = hashlib.sha256(transfer_id.encode()).hexdigest()
        staging = destination.parent / f".yuki-transfer-{suffix}.part"
        data = base64.b64decode(args["data_base64"], validate=True)
        mode = "r+b" if staging.exists() else "x+b"
        with staging.open(mode) as stream:
            stream.seek(args["offset"])
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        result: dict[str, Any] = {
            "transfer_id": transfer_id,
            "path": str(destination),
            "offset": args["offset"],
            "next_offset": args["offset"] + len(data),
            "written": len(data),
            "pending": True,
        }
        if args.get("final"):
            with staging.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if size != args["size"] or digest != args["sha256"]:
                raise DriverError(
                    "file_bytes_mismatch", "Written bytes do not match the original file"
                )
            os.replace(staging, destination)
            result.update(
                pending=False,
                status="succeeded",
                size=size,
                sha256=digest,
                version=file_version(destination.stat()),
            )
        return result

    async def close(self) -> None:
        for task in self.sources.values():
            try:
                source = await task
            except (OSError, DriverError):
                continue
            await asyncio.to_thread(self._close_source, source)
        self.sources.clear()
