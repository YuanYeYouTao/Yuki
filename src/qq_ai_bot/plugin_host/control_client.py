"""Authenticated CLI transport to the running control-plane plugin owner."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx

from qq_ai_bot.config import Settings


@asynccontextmanager
async def plugin_control(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    credential = os.environ.get("YUKI_CONTROL_CREDENTIAL", "")
    if not credential:
        raise ValueError("YUKI_CONTROL_CREDENTIAL is required for online plugin management")
    async with httpx.AsyncClient(
        base_url=settings.webui_origin, timeout=30, headers={"Origin": settings.webui_origin}
    ) as client:
        response = await client.post("/api/control/login", json={"credential": credential})
        response.raise_for_status()
        response = await client.get("/api/control/session")
        response.raise_for_status()
        client.headers["X-Yuki-CSRF"] = response.json()["csrf"]
        yield client


async def plugin_query(
    client: httpx.AsyncClient, method: str, args: dict[str, Any]
) -> dict[str, Any]:
    if method not in {"list_plugins", "read_plugin_approval", "read_plugin_runtime"}:
        raise ValueError("unsupported plugin query")
    response = await client.post(
        f"/api/control/queries/{method}", json=args, headers={"X-Request-ID": str(uuid4())}
    )
    response.raise_for_status()
    return dict(response.json()["data"])


async def plugin_mutation(
    client: httpx.AsyncClient, plugin_id: str, action: str, *, permissions: list[str] | None = None
) -> dict[str, Any]:
    if action not in {"discover", "approve", "enable", "disable", "doctor"}:
        raise ValueError("unsupported plugin action")
    revision = 0
    if action != "discover":
        shown = await plugin_query(client, "read_plugin_approval", {"plugin_id": plugin_id})
        revision = shown["fields"]["revision"]
    if action == "approve" and permissions is None:
        raise ValueError("approval requires explicit --permission values (or an empty list)")
    request_id = str(uuid4())
    payload: dict[str, Any] = {"resource_id": plugin_id, "action": action}
    if permissions is not None:
        payload["spec"] = {"permissions": permissions}
    try:
        response = await client.post(
            "/api/control/commands/mutate_plugin",
            json={
                "request_id": request_id,
                "expected_revision": revision,
                "target": {"kind": "yuki"},
                "payload": payload,
            },
            headers={"X-Request-ID": request_id},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"plugin control request {request_id} unconfirmed; "
            "query original request before retrying"
        ) from exc
    return dict(response.json()["data"])
