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

from yuki.client_sdk.outbox import Outbox


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
        self,
        url: str,
        token: str,
        outbox: Outbox,
        *,
        http: httpx.AsyncClient | None = None,
        entry_binding_id: str | None = None,
        entry_conversation_id: str | None = None,
    ) -> None:
        self.url, self.token, self.outbox = url.rstrip("/"), token, outbox
        self.http = http or httpx.AsyncClient(timeout=30, trust_env=False)
        self._owns_http = http is None
        self._me: dict[str, Any] | None = None
        self._entry_binding_id = entry_binding_id
        self._entry_conversation_id = entry_conversation_id
        self.snapshot: dict[str, Any] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def endpoint(self, path: str) -> str:
        return f"{self.url}/client/v1/{path.lstrip('/')}"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            **({"X-Yuki-Entry": self._entry_binding_id} if self._entry_binding_id else {}),
            **(
                {"X-Yuki-Conversation": self._entry_conversation_id}
                if self._entry_conversation_id
                else {}
            ),
        }

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
        if self._me is None or "request_person_id" not in self._me:
            me = await self.get("me")
            owner = self.outbox.owner()
            if (
                owner is not None
                and self._entry_binding_id is None
                and self._entry_conversation_id is None
            ):
                binding = next(
                    (
                        row["binding_id"]
                        for row in me["bindings"]
                        if row["platform"] == "app"
                        and row["source_person_id"] == owner["person"]
                        and owner["binding_id"] in {None, row["binding_id"]}
                    ),
                    None,
                )
                if owner["server"] != self.url or binding is None:
                    raise ValueError("outbox_owner_mismatch")
                if binding != me["binding_id"]:
                    self._entry_binding_id = binding
                    me = await self.get("me")
            self.outbox.bind(
                self.url,
                me["request_person_id"],
                binding_id=me["binding_id"],
                conversation_id=me["conversation_id"],
            )
            self._entry_binding_id = me["binding_id"]
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

    async def prepare_acp_session(self, *, request_id: str | None = None) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        self.outbox.prepare(identity, "acp/sessions", {"request_id": identity})
        return identity

    async def prepare_soul(
        self, content: str, expected_revision: str, *, request_id: str | None = None
    ) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        self.outbox.prepare(
            identity,
            "character/soul",
            {"request_id": identity, "expected_revision": expected_revision, "content": content},
        )
        return identity

    async def prepare_soul_reload(self, *, request_id: str | None = None) -> str:
        await self._bind()
        identity = request_id or str(uuid4())
        self.outbox.prepare(identity, "character/soul/reload", {"request_id": identity})
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

    async def query(self, identity: str) -> dict[str, Any]:
        """Read the authoritative original receipt without changing local state."""
        await self._bind()
        row = self.outbox.get(identity)
        original = await self.get(f"requests/{identity}")
        if original["found"]:
            self._verify(row, original)
        return original

    async def resolve(self, identity: str) -> dict[str, Any]:
        await self._bind()
        row = self.outbox.get(identity)
        if row["state"] == "draft":
            return row
        original = await self.query(identity)
        if not original["found"]:
            return row
        if row["path"].startswith("character/soul") and original["status"] not in {
            "succeeded",
            "failed",
        }:
            return row
        resolved = {**original, **(original.get("result") or {})}
        resolved.pop("result", None)
        if original.get("problem_code"):
            self.outbox.update(identity, "result", resolved, original["problem_code"])
            raise ClientProblem(original["problem_code"], original.get("problem"))
        state = "accepted"
        if original["operation_kind"] == "message":
            if original.get("turn", {}).get("state") == "ended":
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
        owner = self.outbox.owner()
        assert owner is not None
        body = {
            key: value
            for key, value in row["body"].items()
            if key not in {"request_id", "local_path"}
        }
        if row["path"] == "messages":
            operation, target = "message", owner["conversation_id"]
        elif row["path"] == "acp/sessions":
            operation, target = "client.acp_session_new", owner["binding_id"]
        elif row["path"] == "uploads":
            operation, target = "client.upload", self._entry_conversation_id or owner["person"]
        elif row["path"].startswith("character/soul"):
            reload = row["path"].endswith("/reload")
            digest = hashlib.sha256(
                json.dumps(
                    [
                        "client.character_soul.reload" if reload else "client.character_soul",
                        "soul",
                        body,
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if (
                original["operation_kind"],
                original.get("target"),
                original.get("payload_hash"),
            ) != ("control", "character_soul:reload" if reload else "character_soul", digest):
                raise ClientProblem("request_conflict")
            return
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
                self._verify(row, original)
                if not row["path"].startswith("character/soul") or original["status"] in {
                    "succeeded",
                    "failed",
                }:
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
            elif row["path"] == "character/soul":
                response = await self.http.put(
                    self.endpoint(row["path"]), headers=self.headers, json=row["body"]
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
            "result"
            if row["path"] == "uploads"
            or row["path"].endswith("/cancel")
            or row["path"].startswith("character/soul")
            else "accepted",
            result,
        )
        return self.outbox.get(identity)

    async def refresh(self) -> dict[str, Any]:
        await self._bind()
        home = await self.get("home")
        previous = (
            {
                entry["conversation_id"]: entry
                for entry in (self.snapshot["main"], *self.snapshot["entries"])
            }
            if self.snapshot
            else {}
        )
        collected = {}
        for entry in (home["main"], *home["entries"]):
            identity = entry["conversation_id"]
            original = previous.get(identity)
            unchanged = original is not None and all(
                original[key] == entry[key]
                for key in ("binding_id", "status", "generation", "revision")
            )
            rows = list(self.events.get(identity, ())) if unchanged else []
            if (
                unchanged
                and original is not None
                and original["latest_event_id"] == entry["latest_event_id"]
            ):
                collected[identity] = rows
                continue
            after = rows[-1]["event_id"] if rows else 0
            version = entry["generation"], entry["revision"]
            while True:
                page = await self.get(f"conversations/{identity}/events", after=after, limit=50)
                current = page["generation"], page["revision"]
                if current != version:
                    # An edit, deletion or reset also invalidates the already read prefix.
                    rows, version = [], current
                    if after:
                        after = 0
                        continue
                rows.extend(page["items"])
                for key in ("generation", "revision", "latest_event_id"):
                    entry[key] = page[key]
                if not page["has_more"]:
                    break
                after = page["next_after"]
            collected[identity] = rows
        self.events, self.snapshot = collected, home
        for row in self.outbox.pending():
            if row["state"] in {"submitted", "accepted"}:
                try:
                    await self.resolve(row["request_id"])
                except ClientProblem as exc:
                    if exc.code == "unauthenticated":
                        raise
                    original = self.outbox.get(row["request_id"])
                    self.outbox.update(
                        row["request_id"], original["state"], original["result"], exc.code
                    )
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
