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


async def avatar_mutation(
    client: httpx.AsyncClient,
    action: str,
    png: bytes | None,
    *,
    revision: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Send normalized pixels once; uncertain responses retain the original UUID."""
    import hashlib

    request_id = request_id or str(uuid4())
    if revision is None:
        revision = (await read(client, "character"))["avatar_revision"]
    parameters: dict[str, Any] = {"request_id": request_id, "expected_revision": revision}
    try:
        if action == "reset":
            response = await client.request("DELETE", "/admin/v1/character/avatar", json=parameters)
        else:
            assert png is not None
            response = await client.post(
                "/admin/v1/character/avatar" + ("/default" if action == "set-default" else ""),
                params={**parameters, "sha256": hashlib.sha256(png).hexdigest(), "size": len(png)},
                content=png,
                headers={"Content-Type": "image/png"},
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


async def prompt_mutation(
    client: httpx.AsyncClient,
    key: str | None,
    content: str | None = None,
    *,
    revision: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    payload: dict[str, Any] = {"request_id": request_id}
    path = f"/admin/v1/character/prompts/{key}" if key else "/admin/v1/character/prompts/reload"
    if key:
        if revision is None:
            revision = (await read(client, f"character/prompts/{key}"))["revision"]
        payload.update(expected_revision=revision, content=content)
    try:
        response = await client.request("PUT" if key else "POST", path, json=payload)
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
