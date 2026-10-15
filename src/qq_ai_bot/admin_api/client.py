"""Operator CLI HTTP transport to the existing running Bot."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx

from qq_ai_bot.config import Settings


@asynccontextmanager
async def admin_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    credential = os.environ.get("YUKI_CONTROL_CREDENTIAL", "")
    if not credential:
        raise ValueError("YUKI_CONTROL_CREDENTIAL is required for management commands")
    async with httpx.AsyncClient(
        base_url=settings.admin_api_url,
        timeout=30,
        headers={"Authorization": f"Bearer {credential}"},
    ) as client:
        yield client


async def read(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(f"/admin/v1/{path}", params=params)
    response.raise_for_status()
    return response.json()["data"]


async def mutate(
    client: httpx.AsyncClient,
    path: str,
    revision: int,
    *,
    request_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    try:
        response = await client.post(
            f"/admin/v1/{path}",
            json={
                "request_id": request_id,
                "expected_revision": revision,
                **fields,
            },
        )
        if response.status_code >= 500:
            raise RuntimeError(
                f"request {request_id} unconfirmed; query `admin operation {request_id}`"
            )
        response.raise_for_status()
    except httpx.TransportError as exc:
        raise RuntimeError(
            f"request {request_id} unconfirmed; query `admin operation {request_id}`"
        ) from exc
    result = dict(response.json()["data"])
    if not result["success"]:
        operation = result["operation"]["operation_id"]
        raise RuntimeError(f"request remains unconfirmed; query `admin operation {operation}`")
    return result


async def plugin_mutation(
    client: httpx.AsyncClient,
    identity: str,
    action: str,
    *,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    revision = (
        0 if action == "discover" else (await read(client, f"plugins/{identity}"))["revision"]
    )
    fields: dict[str, Any] = {"permissions": permissions or []} if action == "approve" else {}
    return await mutate(client, f"plugins/{identity}/actions/{action}", revision, **fields)
