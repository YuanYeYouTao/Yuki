"""ACP framing and projection; execution and effect ownership stay on the server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from acp import Client, RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
    UserMessageChunk,
)

from qq_ai_bot import __version__
from qq_ai_bot.client_sdk import ClientProblem, Outbox, PersonalClient

_FAILED_OUTCOMES = frozenset(
    {
        "processing_failure",
        "llm_not_configured",
        "empty_llm_response",
        "llm_failure",
        "capacity_failure",
        "validation_failure",
        "send_or_storage_failure",
        "internal_failure",
    }
)


@dataclass(eq=False)
class _Prompt:
    request_id: str
    task: asyncio.Task[Any]
    submitted: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False


def _request_id(meta: dict[str, Any]) -> str:
    identity = meta.get("yuki.requestId", str(uuid4()))
    try:
        if (
            not isinstance(identity, str)
            or str(UUID(identity)) != identity
            or UUID(identity).version != 4
        ):
            raise ValueError
    except ValueError as exc:
        raise RequestError.invalid_params({"code": "invalid_request_id"}) from exc
    return identity


def _problem(exc: ClientProblem | httpx.HTTPError, identity: str | None = None) -> RequestError:
    data = {"request_id": identity} if identity is not None else {}
    if isinstance(exc, ClientProblem):
        data["code"] = exc.code
        if exc.code == "unauthenticated":
            return RequestError.auth_required(data)
        if exc.code == "not_found":
            return RequestError(-32002, "Resource not found", data)
    return RequestError(-32001, "Yuki request unavailable; query its original request_id", data)


class YukiAgent:
    """One stdio peer; local outboxes retain original HTTP request identities."""

    def __init__(
        self,
        url: str,
        token: str,
        state: Path,
        *,
        entry_binding_id: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self._state = state
        self._base = PersonalClient(
            url,
            token,
            Outbox(state / "sessions.sqlite3"),
            http=self._http,
            entry_binding_id=entry_binding_id,
        )
        self._sessions: dict[str, PersonalClient] = {}
        self._active: dict[str, set[_Prompt]] = {}
        self._capabilities = ClientCapabilities()
        self._conn: Client

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        self._capabilities = client_capabilities or ClientCapabilities()
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(embedded_context=True),
            ),
            agent_info=Implementation(name="yuki", title="Yuki", version=__version__),
        )

    @staticmethod
    def _mcp(mcp_servers: list[Any] | None) -> None:
        if mcp_servers:
            raise RequestError.invalid_params({"code": "unsupported_mcp_servers"})

    async def _session(self, identity: str) -> PersonalClient:
        # Authorization and the actual Binding come from the server on every use.
        try:
            session = await self._base.get(f"acp/sessions/{quote(identity, safe='')}")
        except (ClientProblem, httpx.HTTPError) as exc:
            raise _problem(exc) from None
        conversation = session["conversation_id"]
        if conversation not in self._sessions:
            path = self._state / "conversations" / f"{UUID(conversation).hex}.sqlite3"
            self._sessions[conversation] = PersonalClient(
                self._base.url,
                self._base.token,
                Outbox(path),
                http=self._http,
                entry_binding_id=session["binding_id"],
                entry_conversation_id=conversation,
            )
        return self._sessions[conversation]

    async def _submit(self, client: PersonalClient, identity: str) -> dict[str, Any]:
        try:
            row = await client.submit(identity)
        except httpx.TransportError:
            # The submitted UUID remains durable. Never replace an uncertain POST.
            row = await client.resolve(identity)
            if row["state"] == "submitted":
                raise RequestError(
                    -32001, "Yuki request outcome unknown", {"request_id": identity}
                ) from None
        return cast(dict[str, Any], row["result"])

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        self._mcp(mcp_servers)
        identity = _request_id(kwargs)
        try:
            await self._base.prepare_acp_session(request_id=identity)
            result = await self._submit(self._base, identity)
        except (ClientProblem, httpx.HTTPError) as exc:
            raise _problem(exc, identity) from None
        return NewSessionResponse(session_id=result["session_id"])

    async def _events(
        self, client: PersonalClient, session_id: str, after: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            page = await client.get(f"conversations/{session_id}/events", after=after)
            for event in page["items"]:
                yield event
            if not page["has_more"]:
                return
            after = page["next_after"]

    async def _event_update(
        self, client: PersonalClient, session_id: str, event: dict[str, Any]
    ) -> None:
        blocks: list[TextContentBlock | ResourceContentBlock] = []
        if event["text"]:
            blocks.append(TextContentBlock(type="text", text=event["text"]))
        for attachment in event.get("attachments", ()):
            artifact, version = attachment.get("artifact_id"), attachment.get("version")
            if artifact is not None and version is not None:
                uri = httpx.URL(
                    client.endpoint(f"artifacts/{quote(artifact, safe='')}"),
                    params={"version": version},
                ).copy_with(username=None, password=None)
                blocks.append(
                    ResourceContentBlock(
                        type="resource_link",
                        uri=str(uri),
                        name=attachment.get("filename") or artifact,
                        mime_type=attachment.get("mime_type"),
                        size=attachment.get("size"),
                        description="Download with the original Yuki device credentials.",
                        field_meta={
                            "yuki.conversationId": session_id,
                            "yuki.eventId": event["event_id"],
                        },
                    )
                )
            else:
                # The media ID is real; this API has no independent download route for it.
                blocks.append(
                    TextContentBlock(
                        type="text",
                        text="[Attachment reference; download unavailable] "
                        + json.dumps(attachment, ensure_ascii=False),
                    )
                )
        for content in blocks:
            chunk = (
                UserMessageChunk(session_update="user_message_chunk", content=content)
                if event["direction"] == "inbound"
                else AgentMessageChunk(session_update="agent_message_chunk", content=content)
            )
            await self._conn.session_update(session_id=session_id, update=chunk)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        self._mcp(mcp_servers)
        client = await self._session(session_id)
        try:
            async for event in self._events(client, session_id):
                await self._event_update(client, session_id, event)
        except (ClientProblem, httpx.HTTPError) as exc:
            raise _problem(exc) from None
        return LoadSessionResponse()

    async def _text(self, session_id: str, prompt: list[Any]) -> str:
        parts: list[str] = []
        for block in prompt:
            if isinstance(block, TextContentBlock):
                parts.append(block.text)
            elif isinstance(block, EmbeddedResourceContentBlock) and isinstance(
                block.resource, TextResourceContents
            ):
                parts.append(f"[Provided resource: {block.resource.uri}]\n{block.resource.text}")
            elif isinstance(block, ResourceContentBlock):
                uri = urlsplit(block.uri)
                if uri.scheme == "file":
                    capabilities = self._capabilities.fs
                    if capabilities is None or not capabilities.read_text_file:
                        raise RequestError.invalid_params({"code": "client_file_read_unsupported"})
                    path = unquote(uri.path)
                    if uri.netloc:
                        path = f"//{uri.netloc}{path}"
                    elif len(path) > 3 and path[0] == "/" and path[2] == ":":
                        path = path[1:]
                    resource = await self._conn.read_text_file(path=path, session_id=session_id)
                    parts.append(f"[Provided resource: {block.uri}]\n{resource.content}")
                else:
                    # A URI is a source reference, not evidence of downloaded content.
                    parts.append(f"[Resource link: {block.name}] {block.uri}")
            else:
                raise RequestError.invalid_params({"code": "unsupported_content_type"})
        return "\n\n".join(parts)

    async def _finish(
        self, client: PersonalClient, session_id: str, active: _Prompt
    ) -> PromptResponse:
        sent: set[int] = set()
        while True:
            result = await client.query(active.request_id)
            if not result["found"]:
                raise RequestError(
                    -32001, "Yuki turn outcome unknown", {"request_id": active.request_id}
                )
            wanted = set(result["result_event_ids"])
            async for event in self._events(client, session_id, result["event_id"]):
                if event["direction"] == "outbound" and event["event_id"] in wanted - sent:
                    await self._event_update(client, session_id, event)
                    sent.add(event["event_id"])
            turn, work = result["turn"], result["work"]
            if turn["state"] == "unknown":
                raise RequestError(
                    -32001, "Yuki turn outcome unknown", {"request_id": active.request_id}
                )
            if turn["state"] == "ended" and (
                work is None
                or (
                    not work.get("active", False)
                    and work["state"]
                    in {"completed", "failed", "cancelled", "waiting_user", "suspended"}
                )
            ):
                cancelled = (
                    active.cancelled
                    or turn["outcome"] == "cancelled"
                    or (work is not None and work["state"] == "cancelled")
                )
                if cancelled:
                    return PromptResponse(stop_reason="cancelled")
                if turn["outcome"] in _FAILED_OUTCOMES or (
                    work is not None and work["state"] in {"failed", "suspended"}
                ):
                    raise RequestError(
                        -32001,
                        "Yuki execution did not complete",
                        {
                            "request_id": active.request_id,
                            "outcome": turn["outcome"],
                            "work_state": work["state"] if work is not None else None,
                        },
                    )
                return PromptResponse(stop_reason="end_turn")
            # Read cadence only: silence and elapsed time never complete a turn.
            await asyncio.sleep(1)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        identity = _request_id(kwargs)
        task = asyncio.current_task()
        assert task is not None
        active = _Prompt(identity, task)
        self._active.setdefault(session_id, set()).add(active)
        try:
            client = await self._session(session_id)
            text = await self._text(session_id, prompt)
            await client.prepare_message(text, request_id=identity)
            active.submitted = True
            try:
                await self._submit(client, identity)
            finally:
                active.ready.set()
            return await self._finish(client, session_id, active)
        except asyncio.CancelledError:
            if active.cancelled and not active.submitted:
                return PromptResponse(stop_reason="cancelled")
            raise
        except (ClientProblem, httpx.HTTPError) as exc:
            raise _problem(exc, identity) from None
        finally:
            active.ready.set()
            self._active[session_id].discard(active)
            if not self._active[session_id]:
                del self._active[session_id]

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        for active in tuple(self._active.get(session_id, ())):
            if not active.submitted:
                active.cancelled = True
                active.task.cancel()
                continue
            await active.ready.wait()
            client = await self._session(session_id)
            try:
                result = PersonalClient.data(
                    await self._http.post(
                        client.endpoint(
                            f"acp/sessions/{session_id}/requests/{active.request_id}/cancel"
                        ),
                        headers=client.headers,
                    )
                )
            except (ClientProblem, httpx.HTTPError) as exc:
                raise _problem(exc, active.request_id) from None
            active.cancelled = result["cancelled"]
