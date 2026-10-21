"""Operator CLI HTTP transport to the existing running Bot."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx

from yuki.config import Settings


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


async def _character_write(
    client: httpx.AsyncClient, method: str, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    identity = payload["request_id"]
    try:
        response = await client.request(method, f"/admin/v1/{path}", json=payload)
        if response.status_code >= 500:
            raise RuntimeError(
                f"request {identity} unconfirmed; query `admin operation {identity}`"
            )
        response.raise_for_status()
    except httpx.TransportError as exc:
        raise RuntimeError(
            f"request {identity} unconfirmed; query `admin operation {identity}`"
        ) from exc
    return dict(response.json()["data"])


async def character_mutation(
    client: httpx.AsyncClient, *, request_id: str | None = None, **fields: Any
) -> dict[str, Any]:
    return await _character_write(
        client, "PATCH", "character", {"request_id": request_id or str(uuid4()), **fields}
    )


async def soul_mutation(
    client: httpx.AsyncClient,
    *,
    request_id: str | None = None,
    content: str | None = None,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"request_id": request_id or str(uuid4())}
    if content is not None:
        payload.update(
            content=content,
            expected_revision=expected_revision
            or (await read(client, "character/soul"))["revision"],
        )
    return await _character_write(
        client,
        "PUT" if content is not None else "POST",
        "character/soul" if content is not None else "character/soul/reload",
        payload,
    )


async def mutate(
    client: httpx.AsyncClient,
    path: str,
    revision: int,
    *,
    request_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    result = await _post_mutation(
        client,
        path,
        {
            "request_id": request_id,
            "expected_revision": revision,
            **fields,
        },
    )
    if not result["success"]:
        operation = result["operation"]["operation_id"]
        raise RuntimeError(f"request remains unconfirmed; query `admin operation {operation}`")
    return result


async def identity_mutation(
    client: httpx.AsyncClient, path: str, *, request_id: str | None = None, **fields: Any
) -> dict[str, Any]:
    return await _post_mutation(client, path, {"request_id": request_id or str(uuid4()), **fields})


async def _post_mutation(
    client: httpx.AsyncClient, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    request_id = payload["request_id"]
    try:
        response = await client.post(
            f"/admin/v1/{path}",
            json=payload,
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
    return dict(response.json()["data"])


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
