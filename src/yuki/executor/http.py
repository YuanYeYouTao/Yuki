"""Two distinct Bearer boundaries over one executor domain."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import UUID4

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientPrincipal
from yuki.client_api.models import Envelope, ProblemEnvelope
from yuki.executor.models import (
    Command,
    CreateExecutor,
    ExecutorConfig,
    ExecutorCreated,
    ExecutorOperation,
    ExecutorView,
    Heartbeat,
    HeartbeatResult,
    OperationPage,
    OperationResult,
    ReplaceGrants,
)
from yuki.executor.service import LocalExecutors


def attach_executor_api(
    app: FastAPI, resolve: Callable[[], LocalExecutors], principal: Any
) -> None:
    device = APIRouter(prefix="/client/v1/executors")
    executor = APIRouter(
        prefix="/executor/v1",
        responses={code: {"model": ProblemEnvelope} for code in (401, 404, 409, 422, 500, 503)},
    )
    Actor = Annotated[ClientPrincipal, Depends(principal)]

    async def executor_principal(authorization: Annotated[str | None, Header()] = None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise ClientError("unauthenticated")
        return await resolve().authenticate(token)

    Executor = Annotated[str, Depends(executor_principal)]

    @device.get("", response_model=Envelope[list[ExecutorView]])
    async def listed(actor: Actor) -> Any:
        return {"data": await resolve().list(actor)}

    @device.post("", response_model=Envelope[ExecutorCreated], response_model_exclude_none=True)
    async def create(body: CreateExecutor, actor: Actor) -> Any:
        return {
            "data": await resolve().command(
                actor, str(body.request_id), "create", label=body.label, platform=body.platform
            )
        }

    @device.put("/{identity}/grants", response_model=Envelope[ExecutorConfig])
    async def grants(identity: UUID4, body: ReplaceGrants, actor: Actor) -> Any:
        return {
            "data": await resolve().command(
                actor,
                str(body.request_id),
                "grants",
                str(identity),
                expected_revision=body.expected_revision,
                grants=[g.model_dump(exclude_none=True) for g in body.grants],
            )
        }

    @device.post("/{identity}/{action}", response_model=Envelope[ExecutorConfig])
    async def pause(identity: UUID4, action: str, actor: Actor, body: Command) -> Any:
        if action not in {"pause", "resume"}:
            raise ClientError("not_found")
        return {"data": await resolve().command(actor, str(body.request_id), action, str(identity))}

    @device.delete("/{identity}", response_model=Envelope[ExecutorConfig])
    async def revoke(identity: UUID4, actor: Actor, body: Command) -> Any:
        return {
            "data": await resolve().command(actor, str(body.request_id), "delete", str(identity))
        }

    @device.get("/{identity}/operations", response_model=Envelope[OperationPage])
    async def history(
        identity: UUID4,
        actor: Actor,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> Any:
        return {"data": await resolve().history(actor, str(identity), cursor, limit)}

    @executor.get("/config", response_model=Envelope[ExecutorConfig])
    async def config(actor: Executor) -> Any:
        return {"data": await resolve().config(actor)}

    @executor.get("/stream", responses={200: {"content": {"text/event-stream": {}}}})
    async def stream(actor: Executor) -> StreamingResponse:
        return StreamingResponse(
            resolve().stream(actor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    @executor.post("/heartbeat", response_model=Envelope[HeartbeatResult])
    async def heartbeat(body: Heartbeat, actor: Executor) -> Any:
        return {"data": await resolve().heartbeat(actor, body.local_paused)}

    @executor.get("/operations/{op_id}", response_model=Envelope[ExecutorOperation])
    async def operation(op_id: UUID4, actor: Executor) -> Any:
        return {"data": await resolve().get(actor, str(op_id))}

    @executor.post("/operations/{op_id}/ack", response_model=Envelope[ExecutorOperation])
    async def ack(op_id: UUID4, actor: Executor) -> Any:
        return {"data": await resolve().ack(actor, str(op_id))}

    @executor.post("/operations/{op_id}/result", response_model=Envelope[ExecutorOperation])
    async def result(op_id: UUID4, body: OperationResult, actor: Executor) -> Any:
        return {"data": await resolve().result(actor, str(op_id), body.model_dump())}

    @executor.get(
        "/operations/{op_id}/content",
        responses={
            200: {
                "content": {"application/octet-stream": {}},
                "headers": {
                    "X-Yuki-Sha256": {"schema": {"type": "string"}},
                },
            }
        },
    )
    async def content(op_id: UUID4, actor: Executor) -> Response:
        metadata, data = await resolve().content(actor, str(op_id))
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"X-Yuki-Sha256": metadata["sha256"], "Cache-Control": "no-store"},
        )

    app.include_router(device)
    app.include_router(executor)
