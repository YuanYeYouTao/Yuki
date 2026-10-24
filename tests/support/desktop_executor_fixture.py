"""Loopback-only fault driver over real executor, Main and SQLite code."""

from collections import Counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.container import ApplicationContainer
from yuki.executor.db_models import ExecutorOperationModel
from yuki.runtime.work_schema_v1 import work


def attach_executor_fixture(
    app: FastAPI, container: ApplicationContainer, script: list[Any]
) -> None:
    selected: dict[str, str] = {}
    requests: Counter[str] = Counter()

    @app.middleware("http")
    async def fault(request: Request, call_next):
        suffix = request.url.path
        if "/executor/v1/" in suffix or suffix.endswith("/executors"):
            requests[f"{request.method} {suffix}"] += 1
        match = (
            selected
            and request.method == selected["method"]
            and suffix.endswith(selected["suffix"])
        )
        timing = selected.get("timing") if match else None
        if match:
            selected.clear()
        if timing == "before":
            return JSONResponse({"problem": {"code": "unknown"}}, status_code=503)
        response = await call_next(request)
        if timing == "after":
            return JSONResponse({"problem": {"code": "unknown"}}, status_code=503)
        return response

    @app.post("/__test/executor/loss")
    async def loss(body: dict[str, str]):
        selected.update(method=body["method"], suffix=body["suffix"], timing=body["timing"])
        return {"ok": True}

    @app.post("/__test/executor/script")
    async def queue(body: list[dict[str, Any]]):
        script[:] = [(item["name"], item["arguments"]) for item in body]
        return {"ok": True}

    @app.post("/__test/executor/artifact")
    async def artifact():
        result = snapshot_bytes(
            container.workspace_service.store, "original.txt", b"original immutable bytes"
        )
        return {"artifact_id": result["artifact_id"], "version": result["sha256"]}

    @app.get("/__test/executor/state")
    async def state():
        async with container.database.read_session() as session:
            rows = list(
                await session.scalars(
                    select(ExecutorOperationModel).order_by(
                        ExecutorOperationModel.created_at, ExecutorOperationModel.op_id
                    )
                )
            )
            return {
                "operations": [
                    {
                        "op_id": row.op_id,
                        "tool_call_key": row.tool_call_key,
                        "person_id": row.person_id,
                        "work_id": row.work_id,
                        "state": row.state,
                        "capability": row.capability,
                    }
                    for row in rows
                ],
                "work_count": await session.scalar(select(func.count()).select_from(work)),
                "requests": dict(requests),
            }

    @app.post("/__test/executor/invoke")
    async def invoke(body: dict[str, Any]):
        return await container.local_executors.tools(
            body["person_id"],
            "execute",
            body["arguments"],
            body.get("tool_call_key", f"fixture:{uuid4()}"),
            body.get("work_id"),
        )
