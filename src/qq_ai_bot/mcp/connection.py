"""MCP connection protocol and the official SDK implementation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol, cast

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ToolListChangedNotification

from qq_ai_bot.mcp.models import MCPServerConfig


class MCPConnection(Protocol):
    @property
    def connected(self) -> bool: ...

    @property
    def server_info(self) -> dict[str, str]: ...

    async def connect(self) -> None: ...

    async def list_tools(self) -> tuple[Any, ...]: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> Any: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _ConnectionOperation:
    kind: Literal["list_tools", "call_tool", "close"]
    future: asyncio.Future[Any]
    name: str = ""
    arguments: dict[str, object] | None = None


class SDKMCPConnection:
    """One reusable MCP session backed by the official Python SDK."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._http_transport = http_transport
        self._session: ClientSession | None = None
        self._server_info: dict[str, str] = {}
        self._tools_changed_callback: Callable[[], Awaitable[None]] | None = None
        self._operations: asyncio.Queue[_ConnectionOperation] | None = None
        self._owner_task: asyncio.Task[None] | None = None
        self._active_request: asyncio.Task[Any] | None = None
        self._connect_lock = asyncio.Lock()

    def set_tools_changed_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._tools_changed_callback = callback

    @property
    def connected(self) -> bool:
        return bool(
            self._session is not None
            and self._owner_task is not None
            and not self._owner_task.done()
        )

    @property
    def server_info(self) -> dict[str, str]:
        return dict(self._server_info)

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            loop = asyncio.get_running_loop()
            ready: asyncio.Future[None] = loop.create_future()
            self._operations = asyncio.Queue()
            self._owner_task = asyncio.create_task(
                self._run_owner(ready),
                name="mcp-connection-owner",
            )
            try:
                async with asyncio.timeout(self._connect_timeout):
                    await asyncio.shield(ready)
            except asyncio.CancelledError:
                task, self._owner_task = self._owner_task, None
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._operations = None
                raise
            except Exception:
                task, self._owner_task = self._owner_task, None
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._operations = None
                raise

    async def _run_owner(self, ready: asyncio.Future[None]) -> None:
        """Own SDK contexts so AnyIO always exits them in the entering task."""

        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack)
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self._request_timeout),
                    message_handler=self._handle_message,
                )
            )
            initialized = await session.initialize()
            server = getattr(initialized, "serverInfo", None)
            self._server_info = {
                "protocol_version": str(getattr(initialized, "protocolVersion", "") or ""),
                "server_name": str(getattr(server, "name", "") or ""),
                "server_version": str(getattr(server, "version", "") or ""),
                "server_instructions": str(getattr(initialized, "instructions", "") or ""),
            }
            self._session = session
            if not ready.done():
                ready.set_result(None)
            operations = self._operations
            assert operations is not None
            while True:
                operation = await operations.get()
                if operation.kind == "close":
                    if not operation.future.done():
                        operation.future.set_result(None)
                    break
                try:
                    await self._serve_operation(operation, session)
                finally:
                    # The owner stays suspended at queue.get() while idle. Its last
                    # operation otherwise retains arguments, results and tracebacks.
                    del operation
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            self._cancel_queued_operations()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                self._fail_queued_operations(exc)
        finally:
            self._session = None
            self._active_request = None
            await stack.aclose()

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        if self._config.command is not None:
            parameters = StdioServerParameters(
                command=self._config.command,
                args=list(self._config.args),
                cwd=str(self._config.cwd) if self._config.cwd is not None else None,
                env={**os.environ, **self._config.env},
            )
            return await stack.enter_async_context(stdio_client(parameters))
        assert self._config.url is not None
        client = httpx.AsyncClient(
            headers=self._config.headers,
            timeout=httpx.Timeout(self._request_timeout),
            follow_redirects=False,
            transport=self._http_transport,
        )
        await stack.enter_async_context(client)
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(self._config.url, http_client=client)
        )
        return read, write

    async def _serve_operation(
        self,
        operation: _ConnectionOperation,
        session: ClientSession,
    ) -> None:
        async def perform() -> Any:
            async with asyncio.timeout(self._request_timeout):
                if operation.kind == "list_tools":
                    listed = await session.list_tools()
                    return tuple(listed.tools)
                return await session.call_tool(
                    operation.name,
                    arguments=operation.arguments or {},
                )

        request = asyncio.create_task(perform(), name=f"mcp-{operation.kind}")
        self._active_request = request

        def cancel_request(future: asyncio.Future[Any]) -> None:
            if future.cancelled() and not request.done():
                request.cancel()

        operation.future.add_done_callback(cancel_request)
        try:
            result = await request
        except asyncio.CancelledError:
            if not operation.future.done():
                operation.future.cancel()
        except Exception as exc:
            if not operation.future.done():
                operation.future.set_exception(exc)
        else:
            if not operation.future.done():
                operation.future.set_result(result)
        finally:
            self._active_request = None

    def _fail_queued_operations(self, exc: Exception) -> None:
        operations = self._operations
        if operations is None:
            return
        while not operations.empty():
            operation = operations.get_nowait()
            if operation.future.done():
                continue
            operation.future.set_exception(exc)

    def _cancel_queued_operations(self) -> None:
        operations = self._operations
        if operations is None:
            return
        while not operations.empty():
            operation = operations.get_nowait()
            if not operation.future.done():
                operation.future.cancel()

    async def _handle_message(self, message: Any) -> None:
        root = getattr(message, "root", message)
        if isinstance(root, ToolListChangedNotification) and self._tools_changed_callback:
            await self._tools_changed_callback()

    async def list_tools(self) -> tuple[Any, ...]:
        return cast(
            tuple[Any, ...],
            await self._request(_ConnectionOperation(kind="list_tools", future=self._future())),
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
        return await self._request(
            _ConnectionOperation(
                kind="call_tool",
                future=self._future(),
                name=name,
                arguments=arguments,
            )
        )

    async def close(self) -> None:
        async with self._connect_lock:
            task, operations = self._owner_task, self._operations
            if task is None:
                return
            active = self._active_request
            if active is not None and not active.done():
                active.cancel()
            if operations is not None and not task.done():
                closed = self._future()
                await operations.put(_ConnectionOperation(kind="close", future=closed))
                await closed
            await asyncio.gather(task, return_exceptions=True)
            self._owner_task = None
            self._operations = None

    async def _request(self, operation: _ConnectionOperation) -> Any:
        operations = self._operations
        if not self.connected or operations is None:
            raise RuntimeError("MCP connection is not initialized")
        await operations.put(operation)
        return await operation.future

    @staticmethod
    def _future() -> asyncio.Future[Any]:
        return asyncio.get_running_loop().create_future()


class MCPConnectionFactory(Protocol):
    def __call__(
        self,
        config: MCPServerConfig,
        *,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
    ) -> MCPConnection: ...
