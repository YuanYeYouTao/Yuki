"""Reference personal client: HTTP only, original IDs, snapshot convergence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from qq_ai_bot.client_sdk.outbox import Outbox


class ClientProblem(Exception):
    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        self.code, self.details = code, details or {}
        super().__init__(code)


def _facts(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


class PersonalClient:
    def __init__(
        self, url: str, token: str, outbox: Outbox, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self.url, self.token, self.outbox = url.rstrip("/"), token, outbox
        self.http = http or httpx.AsyncClient(timeout=30, trust_env=False)
        self._owns_http = http is None
        self._me: dict[str, Any] | None = None
        self.snapshot: dict[str, Any] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def endpoint(self, path: str) -> str:
        return f"{self.url}/client/v1/{path.lstrip('/')}"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def data(response: httpx.Response) -> dict[str, Any]:
        payload = response.json()
        if response.is_error:
            problem = payload.get("problem", {})
            raise ClientProblem(problem.get("code", "unknown"), problem)
        return dict(payload["data"])

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.data(
            await self.http.get(self.endpoint(path), headers=self.headers, params=params)
        )

    async def _bind(self) -> None:
        if self._me is None:
            me = await self.get("me")
            self.outbox.bind(self.url, me["person_id"])
            self._me = me

    async def prepare_message(
        self, text: str, attachments: tuple[str, ...] = (), *, request_id: str | None = None
    ) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        self.outbox.prepare(
            identity,
            "messages",
            {"request_id": identity, "text": text.strip(), "attachments": list(attachments)},
        )
        return identity

    async def prepare_input(self, work_id: str, text: str, *, request_id: str | None = None) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        self.outbox.prepare(
            identity, f"works/{work_id}/inputs", {"request_id": identity, "text": text.strip()}
        )
        return identity

    async def prepare_cancel(
        self, work_id: str, revision: int, *, request_id: str | None = None
    ) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        self.outbox.prepare(
            identity,
            f"works/{work_id}/cancel",
            {"request_id": identity, "expected_revision": revision},
        )
        return identity

    async def prepare_upload(
        self, path: Path, kind: str = "file", *, request_id: str | None = None
    ) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        digest, size = await asyncio.to_thread(_facts, path)
        self.outbox.prepare(
            identity,
            "uploads",
            {
                "request_id": identity,
                "filename": path.name,
                "kind": kind,
                "sha256": digest,
                "size": size,
                "local_path": str(await asyncio.to_thread(path.absolute)),
            },
        )
        return identity

    async def resolve(self, identity: str) -> dict[str, Any]:
        await self._bind()
        row = self.outbox.get(identity)
        if row["state"] == "draft":
            return row
        original = await self.get(f"requests/{identity}")
        if not original["found"]:
            return row
        self._verify(row, original)
        resolved = {**original, **(original.get("result") or {})}
        resolved.pop("result", None)
        if original.get("problem_code"):
            self.outbox.update(identity, "result", resolved, original["problem_code"])
            raise ClientProblem(original["problem_code"], original.get("problem"))
        state = "accepted"
        if original["operation_kind"] == "message":
            if original.get("result_event_ids"):
                state = "result"
        elif original["operation_kind"] == "client.work_input":
            item = await self.get(f"works/{original['target']}")
            if item["state"] in {"completed", "failed", "cancelled"}:
                state = "result"
        else:
            state = "result"
        self.outbox.update(identity, state, resolved)
        return self.outbox.get(identity)

    def _verify(self, row: dict[str, Any], original: dict[str, Any]) -> None:
        assert self._me is not None
        body = {
            key: value
            for key, value in row["body"].items()
            if key not in {"request_id", "local_path"}
        }
        if row["path"] == "messages":
            operation, target = "message", self._me["conversation_id"]
        elif row["path"] == "uploads":
            operation, target = "client.upload", self._me["person_id"]
        else:
            _, target, action = row["path"].split("/")
            operation = "client.work_input" if action == "inputs" else "client.work_cancel"
        digest = hashlib.sha256(
            json.dumps([operation, target, body], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (original["operation_kind"], original.get("target"), original.get("payload_hash")) != (
            operation,
            target,
            digest,
        ):
            raise ClientProblem("request_conflict")

    async def submit(self, identity: str) -> dict[str, Any]:
        await self._bind()
        row = self.outbox.get(identity)
        if row["state"] != "draft":
            original = await self.get(f"requests/{identity}")
            if original["found"]:
                return await self.resolve(identity)
        self.outbox.update(identity, "submitted", row["result"])
        try:
            if row["path"] == "uploads":
                params = dict(row["body"])
                path = Path(params.pop("local_path"))
                digest, size = await asyncio.to_thread(_facts, path)
                if (digest, size) != (params["sha256"], params["size"]):
                    self.outbox.update(identity, "draft", problem="local_file_changed")
                    raise ClientProblem("local_file_changed")

                async def chunks() -> AsyncIterator[bytes]:
                    stream = await asyncio.to_thread(path.open, "rb")
                    try:
                        while chunk := await asyncio.to_thread(stream.read, 65536):
                            yield chunk
                    finally:
                        await asyncio.to_thread(stream.close)

                response = await self.http.post(
                    self.endpoint("uploads"), headers=self.headers, params=params, content=chunks()
                )
            else:
                response = await self.http.post(
                    self.endpoint(row["path"]), headers=self.headers, json=row["body"]
                )
            result = self.data(response)
        except ClientProblem as exc:
            if exc.code != "unknown":
                self.outbox.update(
                    identity,
                    "result"
                    if exc.code in {"precondition_failed", "version_conflict"}
                    else "draft",
                    {"problem": exc.details} if exc.details else None,
                    problem=exc.code,
                )
            raise
        # Transport errors keep the durable submitted state: outcome remains unknown.
        self.outbox.update(
            identity,
            "result" if row["path"] == "uploads" or row["path"].endswith("/cancel") else "accepted",
            result,
        )
        return self.outbox.get(identity)

    async def refresh(self) -> dict[str, Any]:
        await self._bind()
        home = await self.get("home")
        collected = {}
        for entry in (home["main"], *home["entries"]):
            identity, after, rows = entry["conversation_id"], 0, []
            while True:
                page = await self.get(f"conversations/{identity}/events", after=after, limit=50)
                rows.extend(page["items"])
                if not page["has_more"]:
                    break
                after = page["next_after"]
            collected[identity] = rows
        self.events, self.snapshot = collected, home
        for row in self.outbox.pending():
            if row["state"] in {"submitted", "accepted"}:
                await self.resolve(row["request_id"])
        return home

    async def watch(self, *, reconnect_delay: float = 1) -> AsyncIterator[dict[str, Any]]:
        await self._bind()
        last_id = ""
        while True:
            try:
                headers = {**self.headers, "Last-Event-ID": last_id}
                async with self.http.stream(
                    "GET", self.endpoint("stream"), headers=headers
                ) as response:
                    if response.is_error:
                        await response.aread()
                        self.data(response)
                    event = ""
                    async for line in response.aiter_lines():
                        if line.startswith("id: "):
                            last_id = line[4:]
                        elif line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: "):
                            # Hints never populate facts; reconnect sync refreshes every entry.
                            json.loads(line[6:])
                            if event in {"sync", "conversation", "work", "activity"}:
                                yield await self.refresh()
            except (httpx.TransportError, httpx.TimeoutException):
                pass
            except ClientProblem as exc:
                if exc.code not in {"unavailable", "unknown"}:
                    raise
            await asyncio.sleep(reconnect_delay)
