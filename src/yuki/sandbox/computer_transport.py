"""Manager streams for container-loopback RFB and mutable workspace snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, cast

if TYPE_CHECKING:
    from yuki.sandbox.persistent import PersistentManager


class ComputerTransport:
    def __init__(self, manager: PersistentManager) -> None:
        self.manager = manager

    def files_list(self, args: dict[str, Any]) -> dict[str, Any]:
        path, after, limit = args.get("path", ""), args.get("after", ""), args.get("limit", 50)
        if not isinstance(after, str) or type(limit) is not int or limit < 1:
            raise ValueError("invalid_file_page")
        parts = self.manager.files.parts(path)
        relative = "/".join(parts)
        result = []
        with self.manager.files.parent(relative + "/.entry" if relative else ".entry") as (
            parent,
            _,
        ):
            names = sorted(name for name in os.listdir(parent) if name > after)
            for name in names[:limit]:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                result.append(
                    {
                        "name": name,
                        "kind": "directory"
                        if stat.S_ISDIR(info.st_mode)
                        else "file"
                        if stat.S_ISREG(info.st_mode)
                        else "link",
                        "size": info.st_size,
                        "mtime": info.st_mtime,
                    }
                )
        return {
            "path": "/workspace" + ("/" + relative if relative else ""),
            "items": result,
            "next_after": names[limit - 1] if len(names) > limit else None,
        }

    def file_snapshot(self, path: str) -> tuple[BinaryIO, dict[str, Any]]:
        snapshot = cast(BinaryIO, tempfile.TemporaryFile())
        try:
            with self.manager.files.open_file(path) as descriptor:
                before, digest, size = os.fstat(descriptor), hashlib.sha256(), 0
                while data := os.read(descriptor, 65536):
                    snapshot.write(data)
                    digest.update(data)
                    size += len(data)
                after = os.fstat(descriptor)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError("file_changed_during_read")
            snapshot.seek(0)
            return snapshot, {
                "sha256": digest.hexdigest(),
                "size": size,
                "name": PurePosixPath(path).name,
            }
        except BaseException:
            snapshot.close()
            raise

    async def files_read(self, path: str, writer: asyncio.StreamWriter) -> None:
        prepared = asyncio.create_task(asyncio.to_thread(self.file_snapshot, path))
        try:
            snapshot, metadata = await asyncio.shield(prepared)
        except asyncio.CancelledError:
            while not prepared.done():
                try:
                    await asyncio.shield(prepared)
                except asyncio.CancelledError:
                    continue
            if not prepared.cancelled() and prepared.exception() is None:
                prepared.result()[0].close()
            raise
        try:
            writer.write(json.dumps(metadata).encode() + b"\n")
            await writer.drain()
            while data := await asyncio.to_thread(snapshot.read, 65536):
                writer.write(data)
                await writer.drain()
        finally:
            snapshot.close()

    async def display_stream(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self.manager.ready:
            writer.write(b'{"error":"environment_unavailable"}\n')
            await writer.drain()
            return
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            self.manager.name,
            "socat",
            "STDIO",
            "TCP:127.0.0.1:5901",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        upstream, downstream = process.stdin, process.stdout

        async def send() -> None:
            while data := await reader.read(65536):
                upstream.write(data)
                await upstream.drain()

        async def receive() -> None:
            while data := await downstream.read(65536):
                writer.write(data)
                await writer.drain()

        tasks: list[asyncio.Task[None]] = []
        try:
            writer.write(b'{"ready":true}\n')
            await writer.drain()
            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream.close()  # EOF reaches socat while the docker client is alive.
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
