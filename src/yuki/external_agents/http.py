"""Connector transport on the existing application, separate from chat credentials."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import and_, or_, select

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.http_errors import install_identity_errors
from yuki.client_api.responses import (
    NativeDirectory,
    NativeOperation,
    NativeOperations,
    NativeSessions,
    TerminalControl,
    TerminalRun,
    TerminalRuns,
)
from yuki.external_agents.db_models import AgentInstanceModel, AgentOperationModel
from yuki.external_agents.service import CONNECTOR_PROTOCOL, ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.identity.credentials import DeviceCredentials, DevicePrincipal, require_person
from yuki.identity.person_identity import person_family_condition


class ConnectorPairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr
    device_label: str


class TerminalControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    action: str


def bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise ClientError("unauthenticated")
    return authorization[7:]


def attach_external_agent_api(app: FastAPI, resolve: Callable[[], ExternalAgentService]) -> None:
    install_identity_errors(app)
    router = APIRouter(prefix="/agents/v1")

    async def person(authorization: str | None) -> DevicePrincipal:
        service = resolve()
        async with service.database.read_session() as session:
            return await DeviceCredentials(service.database).authenticate(
                session, bearer(authorization), purpose="chat"
            )

    @router.post("/pair")
    async def pair(body: ConnectorPairBody) -> dict[str, Any]:
        return await resolve().identity.pair(body.code.get_secret_value(), body.device_label)

    @router.get("/devices", response_model=NativeDirectory, response_model_exclude_unset=True)
    async def devices(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        return {
            "connector_protocol": CONNECTOR_PROTOCOL,
            **await resolve().catalog(principal.source_person_id),
        }

    @router.get(
        "/operations/{operation_id}",
        response_model=NativeOperation,
        response_model_exclude_unset=True,
    )
    async def operation(
        operation_id: str,
        cursor: int = 0,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        try:
            result = await resolve().execute(
                {"action": "get", "operation_id": operation_id, "cursor": cursor},
                f"http-read:{operation_id}:{cursor}",
                {"actor_person_id": principal.source_person_id},
            )
            return {**result, "operation_id": operation_id}
        except ValueError as error:
            if str(error) == "external_agent_not_found":
                raise ClientError("not_found") from None
            raise

    @router.get(
        "/instances/{instance_id}/sessions",
        response_model=NativeSessions,
        response_model_exclude_unset=True,
    )
    async def sessions(
        instance_id: str,
        cursor: str | None = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        try:
            return await resolve().execute(
                {"action": "list", "instance_id": instance_id, "native_cursor": cursor},
                f"http-list:{uuid4()}",
                {"actor_person_id": principal.source_person_id},
            )
        except ValueError as error:
            if str(error) == "external_agent_not_found":
                raise ClientError("not_found") from None
            raise

    @router.get("/operations", response_model=NativeOperations, response_model_exclude_unset=True)
    async def operations(
        work_id: str | None = None,
        device_id: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1)] = 50,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        service = resolve()
        previous = None
        if cursor is not None:
            try:
                previous = await service.repository.operation(cursor, principal.source_person_id)
            except ValueError:
                raise ClientError("not_found") from None
        async with service.database.read_session() as reader:
            actor = await require_person(reader, principal.source_person_id)
            statement = (
                select(AgentOperationModel)
                .join(AgentInstanceModel, AgentInstanceModel.id == AgentOperationModel.instance_id)
                .join(ClientDeviceModel, ClientDeviceModel.id == AgentInstanceModel.device_id)
                .where(person_family_condition(ClientDeviceModel.person_id, actor))
                .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
            )
            if work_id is not None:
                statement = statement.where(AgentOperationModel.work_id == work_id)
            if device_id is not None:
                statement = statement.where(AgentInstanceModel.device_id == device_id)
            if previous is not None:
                statement = statement.where(
                    or_(
                        AgentOperationModel.created_at > previous.created_at,
                        and_(
                            AgentOperationModel.created_at == previous.created_at,
                            AgentOperationModel.id > previous.id,
                        ),
                    )
                )
            rows = list(await reader.scalars(statement.limit(limit + 1)))
        items = [
            {
                **await service._view(row),
                "operation_id": row.id,
                "work_id": row.work_id,
                "action": row.action,
            }
            for row in rows[:limit]
        ]
        return {"items": items, "next_cursor": rows[limit - 1].id if len(rows) > limit else None}

    @router.get("/devices/{device_id}")
    async def device_status(
        device_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        return await resolve().local_devices.status(device_id, principal.source_person_id)

    @router.get("/terminals", response_model=TerminalRuns, response_model_exclude_unset=True)
    async def terminals(
        device_id: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1)] = 50,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        return await resolve().local_devices.list_runs(
            principal.source_person_id,
            device_id=device_id,
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/terminals/{run_id}", response_model=TerminalRun, response_model_exclude_unset=True
    )
    async def terminal(
        run_id: str,
        cursor: int = 0,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        return await resolve().local_devices.read(principal.source_person_id, run_id, cursor)

    @router.post(
        "/terminals/{run_id}/control",
        response_model=TerminalControl,
        response_model_exclude_unset=True,
    )
    async def terminal_control(
        run_id: str,
        body: TerminalControlBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        if body.action != "cancel":
            raise ClientError("invalid_request")
        return await resolve().local_devices.control(
            principal.source_person_id,
            run_id,
            body.request_id,
            body.action,
        )

    @router.get(
        "/terminals/{run_id}/controls/{request_id}",
        response_model=TerminalControl,
        response_model_exclude_unset=True,
    )
    async def terminal_control_receipt(
        run_id: str,
        request_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await person(authorization)
        return await resolve().local_devices.control_receipt(
            principal.source_person_id,
            run_id,
            request_id,
        )

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
                message = await queue.get()
                if message["type"] == "catalog":
                    instances = message.get("instances")
                    if not isinstance(instances, list):
                        raise ValueError("invalid_connector_catalog")
                    await service.update_catalog(
                        principal.device_id,
                        connection,
                        instances,
                        message.get("device_capabilities", {}),
                    )
                    await connection.send({"type": "catalog_ack"})
                else:
                    # Acknowledge each original durable event
                    # only after its complete transaction. Explicit repository lists
                    # remain an atomic batch and are never divided here.
                    if message["type"] == "terminal_event":
                        await service.local_devices.receive(principal.device_id, message)
                    else:
                        await service.receive_events(principal.device_id, [message])
                    await connection.send({"type": "ack", "event_id": message["event_id"]})

        async def read() -> None:
            hello = await socket.receive_json()
            if hello.get("type") != "hello":
                raise ValueError("connector_hello_required")
            try:
                await service.connect(principal.device_id, connection, hello)
            except ValueError as error:
                if str(error) == "unsupported_connector_protocol":
                    await socket.close(code=1008, reason="unsupported_connector_protocol")
                raise
            while True:
                message = await socket.receive_json()
                kind = message.get("type")
                if kind == "response":
                    connection.respond(message)
                elif kind in {"event", "catalog", "terminal_event"}:
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
            await service.disconnect(connection)

    app.include_router(router)
