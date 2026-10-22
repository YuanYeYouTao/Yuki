"""Connector transport on the existing application, separate from chat credentials."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Header, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, SecretStr

from yuki.client_api.errors import ClientError
from yuki.client_api.http_errors import install_identity_errors
from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.identity.credentials import DeviceCredentials, DevicePrincipal


class ConnectorPairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr
    device_label: str


def bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise ClientError("unauthenticated")
    return authorization[7:]


def attach_external_agent_api(app: FastAPI, resolve: Callable[[], ExternalAgentService]) -> None:
    install_identity_errors(app)
    router = APIRouter(prefix="/agents/v1")

    async def person(authorization: str | None) -> DevicePrincipal:
        service = resolve()
        async with service.database.sessions() as session:
            return await DeviceCredentials(service.database).authenticate(
                session, bearer(authorization), purpose="chat"
            )

    @router.post("/pair")
    async def pair(body: ConnectorPairBody) -> dict[str, Any]:
        return await resolve().identity.pair(body.code.get_secret_value(), body.device_label)

    @router.get("/devices")
    async def devices(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        return await resolve().catalog(principal.source_person_id)

    @router.get("/operations/{operation_id}")
    async def operation(
        operation_id: str,
        cursor: int = 0,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        try:
            return await resolve().execute(
                {"action": "get", "operation_id": operation_id, "cursor": cursor},
                f"http-read:{operation_id}:{cursor}",
                {"actor_person_id": principal.source_person_id},
            )
        except ValueError as error:
            if str(error) == "external_agent_not_found":
                raise ClientError("not_found") from None
            raise

    @router.websocket("/connect")
    async def connect(socket: WebSocket) -> None:
        service = resolve()
        try:
            principal = await service.identity.authenticate(
                bearer(socket.headers.get("authorization"))
            )
        except ClientError:
            await socket.close(code=4401)
            return
        await socket.accept()
        connection = DeviceConnection(principal.device_id, socket)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def events() -> None:
            while True:
                batch = [await queue.get()]
                while not queue.empty():
                    batch.append(queue.get_nowait())
                await service.receive_events(principal.device_id, batch)
                for event in batch:
                    await connection.send({"type": "ack", "event_id": event["event_id"]})

        async def read() -> None:
            hello = await socket.receive_json()
            if hello.get("type") != "hello":
                raise ValueError("connector_hello_required")
            await service.connect(principal.device_id, connection, hello)
            while True:
                message = await socket.receive_json()
                kind = message.get("type")
                if kind == "response":
                    connection.respond(message)
                elif kind == "event":
                    queue.put_nowait(message)
                elif kind == "ping":
                    await connection.send({"type": "pong"})
                else:
                    raise ValueError("invalid_connector_message")

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(read())
                group.create_task(events())
        except* WebSocketDisconnect:
            pass
        except* (ValueError, ClientError):
            with suppress(RuntimeError):
                await socket.close(code=1008)
        finally:
            service.connections.detach(connection)

    app.include_router(router)
