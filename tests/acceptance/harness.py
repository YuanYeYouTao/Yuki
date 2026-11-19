"""In-process real Bot (container, SQLite, Main, Work, Client API, device WSS) for phase 2.

Only the Main model is scripted (it chooses tool calls); the native agents, the
connectors (two pairings = two "devices" on this Mac), the device protocol, the
journal and every receipt are real. Group traffic enters through the real Telegram
ingress with an offline HTTP transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal

from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.external_agents.http import attach_external_agent_api
from yuki.identity.canonical_repository import (
    create_presence,
    ensure_account,
    replace_assignment,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.database import Database
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import inputs, work

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "test-results" / "external-agent-phase2-acceptance"
SCRATCH = Path(
    os.environ.get(
        "YUKI_ACCEPTANCE_SCRATCH", str(Path(tempfile.gettempdir()) / "yuki-phase2-acceptance")
    )
)
TMUX = ["tmux", "-L", os.environ.get("YUKI_ACCEPTANCE_TMUX", "p2acc")]
OWNER_TG = 42
GROUP_CHAT = -10042


def call(name: str, arguments: dict[str, Any]) -> ChatResponse:
    return ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
    )


def say(text: str) -> ChatResponse:
    return call("send", {"text": text})


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Native agent installs (scratch only, never global; see docs/operations commands)
# ---------------------------------------------------------------------------


def codex_socket() -> str:
    """The resolved socket of OUR app-server (never ~/.codex/app-server-control)."""
    link = SCRATCH / "codex.sock"
    resolved = os.path.realpath(link)
    assert "app-server-control" not in resolved, resolved
    assert os.path.exists(resolved), "start the private app-server first (commands.sh)"
    return resolved


# The connector's read-only Claude discovery runs `python -m yuki_agent_connector...` in a
# child process; an installed connector has it on sys.path, this in-process one needs it.
CONNECTOR_SRC = str(REPO / "packages" / "yuki-agent-connector" / "src")


def claude_instance(identity: str, cwd: Path) -> Instance:
    return Instance(
        identity,
        "claude",
        "Claude",
        "claude",
        cwd=str(cwd),
        env={"ANTHROPIC_MODEL": "haiku", "PYTHONPATH": CONNECTOR_SRC},
    )


def codex_instance(identity: str, cwd: Path) -> Instance:
    return Instance(identity, "codex", "Codex", "codex", cwd=str(cwd), socket=codex_socket())


def acp_instance(identity: str, cwd: Path) -> Instance:
    binary = SCRATCH / "acp" / "node_modules" / ".bin" / "claude-agent-acp"
    assert binary.exists(), "npm install --prefix <scratch>/acp claude-agent-acp first"
    return Instance(
        identity, "acp", "Claude ACP", str(binary), cwd=str(cwd), env={"ANTHROPIC_MODEL": "haiku"}
    )


def project(name: str) -> Path:
    path = SCRATCH / "proj" / name
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


# ---------------------------------------------------------------------------
# Facts recorder (no message bodies)
# ---------------------------------------------------------------------------


@dataclass
class Facts:
    started: float = field(default_factory=now)
    model: list[dict[str, Any]] = field(default_factory=list)
    admission: list[dict[str, Any]] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)

    def mark(self, name: str, **values: Any) -> None:
        self.marks.append({"t": round(now() - self.started, 3), "mark": name, **values})


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------

Decide = Callable[["Turn"], Awaitable[ChatResponse | None]]


@dataclass
class Turn:
    """One Main model request as the scripted model sees it."""

    stack: Stack
    request: Any
    control: Any
    work_id: str | None
    trigger_text: str
    trigger_sender: str | None
    last_role: str
    last_text: str

    def last_tool(self) -> dict[str, Any] | None:
        if self.last_role != "tool":
            return None
        with contextlib.suppress(ValueError):
            return json.loads(self.last_text)
        return {"raw": self.last_text}


class Stack:
    def __init__(self, root: Path, facts: Facts) -> None:
        self.root = root
        self.facts = facts
        self.decide: Decide | None = None
        self.runtime: ApplicationContainer | None = None
        self.database: Database | None = None
        self.server: uvicorn.Server | None = None
        self.serving: asyncio.Task[None] | None = None
        self.port = _free_port()
        self.wire: list[tuple[str, dict[str, Any]]] = []
        self.connectors: dict[str, SimpleNamespace] = {}
        self.update_id = 0
        self.warm: set[int] = set()
        self.state: dict[str, Any] = {}
        self.gateway: Any = None
        self.http_client: httpx.AsyncClient | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/agents/v1/connect"

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        root = self.root
        import logging

        if not getattr(self, "_log_handler", None):
            handler = logging.FileHandler(root / "bot.log")
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
            )
            handler.setLevel(logging.INFO)
            for name in ("yuki", "yuki_agent_connector"):
                logging.getLogger(name).addHandler(handler)
                logging.getLogger(name).setLevel(logging.INFO)
            self._log_handler = handler
        (root / "character").mkdir(parents=True, exist_ok=True)
        soul = root / "character" / "SOUL.md"
        if not soul.exists():
            soul.write_text("你是 {{name}}。\n", encoding="utf-8")
        database_path = root / "yuki.sqlite3"
        first = not database_path.exists()
        self.database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        runtime = ApplicationContainer(
            make_settings(
                self.database.url,
                soul_file=soul,
                model_profiles_file=write_fake_profiles(root / "models.toml"),
                plugin_system_enabled=False,
                plugin_directory=root / "plugins",
                workspace_directory=root / "workspace",
                conversation_media_cache_directory=root / "media",
                social_transfer_directory=root / "transfer",
                runtime_work_enabled=True,
                automation_enabled=False,
                self_memory_enabled=False,
                asr_enabled=False,
                web_mode="disabled",
            ),
            database=self.database,
        )
        self.runtime = runtime
        if first:
            await self.database.create_schema()
            await runtime.character.initialize("初始角色")
            await self._seed_identity()
        self._install_model()
        await runtime.model_invocations.writer.start()
        await runtime.app_gateway.start()
        await runtime._freeze_main_manifest()
        await runtime.work_scheduler.start()
        await self._start_telegram()
        api = FastAPI()
        attach_client_api(api, lambda: runtime.client_api)
        attach_external_agent_api(api, lambda: runtime.external_agents)
        config = uvicorn.Config(
            api, host="127.0.0.1", port=self.port, log_level="critical", timeout_graceful_shutdown=1
        )
        self.server = uvicorn.Server(config)
        self.serving = asyncio.create_task(self.server.serve())
        async with asyncio.timeout(15):
            while not self.server.started:
                if self.serving.done():
                    self.serving.result()
                await asyncio.sleep(0.02)
        self.http_client = httpx.AsyncClient(base_url=self.base, trust_env=False, timeout=30)
        if first:
            await self._pair_client()

    async def stop(self, *, graceful: bool = True) -> None:
        """Stop the Bot (the connectors and native agents keep running)."""
        assert self.runtime is not None and self.database is not None
        if self.http_client is not None:
            await self.http_client.aclose()
        if self.gateway is not None:
            await self.gateway.close()
        if self.server is not None:
            self.server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.serving, 10)
        await self.runtime.work_scheduler.close()
        await self.runtime.lifecycle._close_entries(self.runtime.lifecycle._entries)
        await self.database.close()
        self.runtime = None

    async def restart(self) -> None:
        self.facts.mark("bot_stop")
        await self.stop()
        self.facts.mark("bot_start")
        await self.start()

    # ---- identity ------------------------------------------------------------------

    async def _seed_identity(self) -> None:
        runtime = self.runtime
        assert runtime is not None and self.database is not None
        owner, operator = str(uuid4()), str(uuid4())
        stamp = datetime.now(UTC)
        async with self.database.immediate_session() as session:
            session.add(
                CanonicalPersonModel(
                    id=owner,
                    display_name="Owner",
                    is_superuser=True,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            await session.flush()
            account = await ensure_account(session, str(OWNER_TG), platform="telegram", now=stamp)
            person = await session.get(CanonicalPersonModel, owner)
            await replace_assignment(session, account, None, person, now=stamp)
            await create_presence(
                session, external_account_id="yuki-app", platform="app", now=stamp
            )
            presence = await create_presence(
                session, external_account_id="80001", platform="telegram", now=stamp
            )
        self.state.update(owner=owner, operator=operator, presence=presence.id)
        (self.root / "state.json").write_text(json.dumps(self.state))

    async def _pair_client(self) -> None:
        runtime = self.runtime
        assert runtime is not None and self.http_client is not None
        code = await runtime.client_identity.issue_for_operator(
            self.state["owner"], self.state["operator"], str(uuid4())
        )
        paired = await self.http_client.post(
            "/client/v1/pair", json={"code": code["code"], "device_label": "Owner Desktop"}
        )
        assert paired.status_code == 200, paired.text
        self.state["client_token"] = paired.json()["data"]["token"]
        (self.root / "state.json").write_text(json.dumps(self.state))

    def load_state(self) -> None:
        self.state = json.loads((self.root / "state.json").read_text())

    @property
    def client_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.state["client_token"]}

    async def pair_device(self, label: str) -> dict[str, Any]:
        runtime = self.runtime
        assert runtime is not None and self.http_client is not None
        grant = await runtime.client_identity.issue_for_operator(
            self.state["owner"], self.state["operator"], str(uuid4()), purpose="connector"
        )
        paired = await self.http_client.post(
            "/agents/v1/pair", json={"code": grant["code"], "device_label": label}
        )
        assert paired.status_code == 200, paired.text
        return dict(paired.json())

    # ---- connectors (one per "device") -----------------------------------------------

    async def start_connector(
        self,
        name: str,
        device: dict[str, Any],
        instances: list[Instance],
        *,
        url: str | None = None,
    ) -> SimpleNamespace:
        journal_dir = self.root / "connectors" / name
        journal_dir.mkdir(parents=True, exist_ok=True)
        connector = Connector(
            Config(url or self.ws_url, device["device_id"], device["token"], instances=instances),
            Journal(journal_dir / "journal.sqlite3", namespace=device["device_id"]),
        )
        task = asyncio.create_task(connector.run(), name=f"connector-{name}")
        entry = SimpleNamespace(name=name, device=device, connector=connector, task=task)
        self.connectors[name] = entry
        self.facts.mark("connector_start", device=name)
        return entry

    async def stop_connector(self, name: str) -> None:
        entry = self.connectors.pop(name)
        entry.connector.stopped.set()
        entry.task.cancel()
        if entry.connector.stream is not None:
            with contextlib.suppress(Exception):
                await entry.connector.stream.close()
        await asyncio.gather(entry.task, return_exceptions=True)
        entry.connector.journal.close()
        self.facts.mark("connector_stop", device=name)

    async def online_instances(self, device_id: str, count: int) -> list[dict[str, Any]]:
        runtime = self.runtime
        assert runtime is not None

        async def ready() -> list[dict[str, Any]] | None:
            catalog = await runtime.external_agents.catalog(self.state["owner"])
            rows = [
                item
                for item in catalog["instances"]
                if item["device_id"] == device_id and item["online"]
            ]
            return rows if len(rows) >= count else None

        return await until(ready, 120)

    # ---- group traffic ---------------------------------------------------------------

    async def _start_telegram(self) -> None:
        from yuki.gateway.providers.telegram import TelegramClient
        from yuki.gateway.providers.telegram_ingress import TelegramGateway

        runtime = self.runtime
        assert runtime is not None
        if not self.state:
            self.load_state()

        def http(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            params = json.loads(request.content or b"{}")
            self.wire.append((method, params))
            result: Any = True
            if method == "getMe":
                result = {"id": 80001, "is_bot": True, "username": "yuki_test"}
            elif method == "getChatMember":
                result = {"status": "administrator", "user": {"id": params.get("user_id")}}
            elif method == "sendMessage":
                result = {"message_id": 5000 + len(self.wire), "chat": {"id": params["chat_id"]}}
            return httpx.Response(200, json={"ok": True, "result": result})

        transport = httpx.AsyncClient(transport=httpx.MockTransport(http))
        client = TelegramClient("offline-acceptance-token", client=transport)
        await client.get_me()
        runtime.gateway_registry.connect(
            client, provider_id="telegram", presence_id=self.state["presence"]
        )
        self.gateway = TelegramGateway(
            runtime.database,
            runtime.gateway_registry,
            runtime.presence_router,
            runtime.processor,
            runtime.scoped_events,
            client,
        )
        self.gateway.presence_id = self.state["presence"]

    async def group(
        self,
        sender: int,
        text: str,
        name: str = "P",
        chat: int = GROUP_CHAT,
        *,
        direct: bool = True,
    ) -> int:
        """One group message from Telegram user ``sender``; returns its update id.

        ``direct`` @-mentions the bot (a request addressed to Yuki); otherwise it is
        ordinary group traffic, which the group's own quiet-window batching admits.
        """
        if chat not in self.warm:
            # The first message of a never-seen Telegram group is sometimes dropped by the
            # ingress (ProcessResult reason "ordinary_source_changed"; reported, not part
            # of phase 2). One warm-up message lets the group binding settle first.
            self.warm.add(chat)
            self.facts.mark("group_warmup", chat=chat)
            await self.group(99, "!ai 大家好", "W", chat, direct=False)
            await self.drain_group()
            await asyncio.sleep(5)
        self.update_id += 1
        identity = self.update_id + int(now()) % 100000 * 100
        message: dict[str, Any] = {
            "message_id": identity,
            "date": int(now()),
            "from": {"id": sender, "first_name": name, "is_bot": False},
            "chat": {"id": chat, "type": "supergroup", "title": f"G{chat}"},
            "text": text,
        }
        if direct:
            message["text"] = "@yuki_test " + text
            message["entities"] = [{"type": "mention", "offset": 0, "length": 10}]
        await self.gateway.receive({"update_id": identity, "message": message})
        return identity

    def sent(self) -> list[dict[str, Any]]:
        return [params for method, params in self.wire if method == "sendMessage"]

    async def drain_group(self) -> None:
        if self.gateway._turns:
            await asyncio.gather(*tuple(self.gateway._turns), return_exceptions=True)

    # ---- the scripted Main model ------------------------------------------------------

    def _install_model(self) -> None:
        from tests.integration.test_current_material_main_contract import _catalog, _profile
        from yuki.model_runtime.pool import ModelClientPool

        stack = self

        class ScriptedMain(FakeLLMProvider):
            async def _complete(self, request: Any) -> ChatResponse:
                return await stack._respond(request)

        runtime = self.runtime
        assert runtime is not None
        runtime.models.apply_catalog(
            _catalog(_profile()),
            ModelClientPool(injected_profiles={"main": ScriptedMain(lambda r: "unused")}),
        )
        executor = runtime.models
        original = executor._execute_provider

        async def admitted(provider: Any, request: Any, *, priority: Any) -> ChatResponse:
            control = current_work_control.get()
            entry = {
                "t_enter": round(now() - stack.facts.started, 3),
                "priority": getattr(priority, "name", str(priority)),
                "work_id": control.current["id"] if control and control.current else None,
            }
            stack.facts.admission.append(entry)
            token = _ADMISSION.set(entry)
            try:
                return await original(provider, request, priority=priority)
            finally:
                _ADMISSION.reset(token)

        executor._execute_provider = admitted

    async def _respond(self, request: Any) -> ChatResponse:
        entry = _ADMISSION.get()
        started = now()
        if entry is not None:
            entry["t_admitted"] = round(started - self.facts.started, 3)
            entry["admission_wait_s"] = round(entry["t_admitted"] - entry["t_enter"], 3)
        control = current_work_control.get()
        trigger_text, sender = "", None
        if control is not None and control.source.get("trigger_event_id"):
            from yuki.persistence.models import ChatEventModel

            assert self.database is not None
            async with self.database.read_session() as reader:
                event = await reader.get(ChatEventModel, control.source["trigger_event_id"])
            if event is not None:
                trigger_text = event.content or ""
                sender = event.sender_user_id
        last = request.messages[-1]
        turn = Turn(
            self,
            request,
            control,
            control.current["id"] if control and control.current else None,
            trigger_text,
            sender,
            last.role,
            last.content or "",
        )
        response = None
        if self.decide is not None:
            try:
                response = await self.decide(turn)
            except BaseException as error:
                import traceback

                self.facts.marks.append({"mark": "decide_error", "error": repr(error)})
                traceback.print_exc()
                raise
        if response is None:
            response = _default(turn)
        names = [item.function.name for item in response.tool_calls or ()]
        lease = getattr(control, "lease", None)
        session = getattr(control, "session", None)
        self.facts.model.append(
            {
                "t": round(started - self.facts.started, 3),
                "activation": f"{lease.owner}:{lease.fence}" if lease is not None else None,
                "input_ids": list(session.input_ids) if session is not None else [],
                "work_id": turn.work_id,
                "trigger_event_id": control.source.get("trigger_event_id") if control else None,
                "last_role": last.role,
                "tools": names,
                "tool_args_keys": [
                    sorted(json.loads(item.function.arguments))
                    for item in response.tool_calls or ()
                ],
            }
        )
        return response

    # ---- reads -------------------------------------------------------------------------

    async def read(self, statement: Any) -> list[Any]:
        assert self.database is not None
        async with self.database.read_session() as reader:
            return list((await reader.execute(statement)).mappings().all())

    async def work_row(self, identity: str) -> dict[str, Any]:
        rows = await self.read(select(work).where(work.c.id == identity))
        return dict(rows[0])

    async def operations(self, work_id: str) -> list[dict[str, Any]]:
        assert self.database is not None
        async with self.database.read_session() as reader:
            rows = list(
                await reader.scalars(
                    select(AgentOperationModel)
                    .where(AgentOperationModel.work_id == work_id)
                    .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
                )
            )
            result = []
            for row in rows:
                outcome = json.loads(row.result_json) if row.result_json else None
                result.append(
                    {
                        "operation_id": row.id,
                        "action": row.action,
                        "run_id": row.run_id,
                        "session_id": row.session_id,
                        "execution_id": row.native_execution_id,
                        "actor_person_id": row.actor_person_id,
                        "detached": row.detached_at is not None,
                        "status": (outcome or {}).get("status"),
                        "pending": (outcome or {}).get("pending"),
                        "error": (outcome or {}).get("error"),
                        "state_sequence": row.state_sequence
                        if hasattr(row, "state_sequence")
                        else None,
                    }
                )
            return result

    async def events(self, work_id: str) -> list[dict[str, Any]]:
        assert self.database is not None
        async with self.database.read_session() as reader:
            rows = list(
                await reader.execute(
                    select(AgentEventModel)
                    .join(
                        AgentOperationModel,
                        AgentOperationModel.id == AgentEventModel.operation_id,
                    )
                    .where(AgentOperationModel.work_id == work_id)
                    .order_by(AgentEventModel.id)
                )
            )
            result = []
            for (row,) in rows:
                payload = json.loads(row.payload_json).get("payload") or {}
                result.append(
                    {
                        "cursor": row.id,
                        "event_id": row.event_id,
                        "operation_id": row.operation_id,
                        "kind": row.kind,
                        "status": payload.get("status") if isinstance(payload, dict) else None,
                        "reason": payload.get("reason") if isinstance(payload, dict) else None,
                        "tool": _tool_name(payload.get("tool"))
                        if isinstance(payload, dict)
                        else None,
                        "observed": payload.get("observed") if isinstance(payload, dict) else None,
                        "answer_at": payload.get("answer_at")
                        if isinstance(payload, dict)
                        else None,
                        "permission_resolved": isinstance(payload, dict)
                        and "permission_resolved" in payload,
                        "native_method": payload.get("native_method")
                        if isinstance(payload, dict)
                        else None,
                        "execution_status": payload.get("execution_status")
                        if isinstance(payload, dict)
                        else None,
                        "received_at": row.created_at.isoformat(),
                    }
                )
            return result

    async def inputs_of(self, work_id: str) -> list[dict[str, Any]]:
        rows = await self.read(
            select(inputs).where(inputs.c.work_id == work_id).order_by(inputs.c.id)
        )
        return [
            {
                "input_id": row["id"],
                "kind": row["kind"],
                "source_key": row["source_key"],
                "state": row["state"],
                "event_id": row["event_id"],
                "created": row["created"],
            }
            for row in rows
        ]

    async def children_of(self, work_id: str) -> list[dict[str, Any]]:
        rows = await self.read(
            select(work.c.id, work.c.state, work.c.reason, children.c.executor_json)
            .join(children, children.c.work_id == work.c.id)
            .where(work.c.parent_work_id == work_id)
            .order_by(work.c.created, work.c.id)
        )
        return [
            {
                "work_id": row["id"],
                "state": row["state"],
                "reason": row["reason"],
                "executor": json.loads(row["executor_json"]) if row["executor_json"] else None,
            }
            for row in rows
        ]

    async def card(self, work_id: str) -> dict[str, Any] | None:
        from yuki.external_agents.delegation import Delegations

        assert self.database is not None
        async with self.database.read_session() as reader:
            return await Delegations(self.database).card(reader, work_id)


_ADMISSION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "acceptance_admission", default=None
)


def _default(turn: Turn) -> ChatResponse:
    """Ordinary replies: a message gets one send; after a receipt the turn ends."""
    if turn.last_role == "tool":
        control = turn.control
        if control is not None and control.current is not None and not control.ending:
            return call("manage_task", {"action": "finish", "result": "done"})
        return ChatResponse(content="", latency_seconds=0)
    return say("收到")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def until(read: Callable[[], Awaitable[Any]], limit: float = 60, every: float = 0.25) -> Any:
    async with asyncio.timeout(limit):
        while True:
            value = await read()
            if value:
                return value
            await asyncio.sleep(every)


def write_artifact(name: str, payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    return path


class StateRecorder:
    """Every observed (state, reason) transition of the watched Works, with times."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.ids: set[str] = set()
        self.sequence: list[dict[str, Any]] = []
        self.last: dict[str, tuple[Any, Any]] = {}
        self.task: asyncio.Task[None] | None = None

    def watch(self, *ids: str) -> None:
        self.ids.update(ids)

    async def _poll(self) -> None:
        while True:
            if self.ids and self.stack.database is not None and self.stack.runtime is not None:
                with contextlib.suppress(Exception):
                    rows = await self.stack.read(
                        select(work.c.id, work.c.state, work.c.reason).where(
                            work.c.id.in_(tuple(self.ids))
                        )
                    )
                    for row in rows:
                        value = (row["state"], row["reason"])
                        if self.last.get(row["id"]) != value:
                            self.last[row["id"]] = value
                            self.sequence.append(
                                {
                                    "t": round(now() - self.stack.facts.started, 3),
                                    "work_id": row["id"],
                                    "state": value[0],
                                    "reason": value[1],
                                }
                            )
            await asyncio.sleep(0.1)

    def start(self) -> None:
        self.task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def of(self, identity: str) -> list[dict[str, Any]]:
        return [item for item in self.sequence if item["work_id"] == identity]


class NetworkGate:
    """A loopback TCP relay in front of the Bot for one device: cut() = network down.

    The connector process and its native agents keep running (as on a real network
    drop); reconnect attempts are refused until restore().
    """

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.port = _free_port()
        self.open = True
        self.links: set[asyncio.Task[None]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", self.port)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.open:
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.target_port
            )
        except OSError:
            writer.close()
            return
        self.writers.update((writer, upstream_writer))

        async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                sink.close()

        for task in (
            asyncio.create_task(pump(reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, writer)),
        ):
            self.links.add(task)
            task.add_done_callback(self.links.discard)

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/agents/v1/connect"

    async def cut(self) -> None:
        self.open = False
        for writer in list(self.writers):
            writer.transport.abort()
        self.writers.clear()

    def restore(self) -> None:
        self.open = True

    async def close(self) -> None:
        await self.cut()
        if self.server is not None:
            self.server.close()


# ---------------------------------------------------------------------------
# Private tmux ("the user's terminal")
# ---------------------------------------------------------------------------


def tmux(*args: str) -> str:
    return subprocess.run([*TMUX, *args], check=True, capture_output=True, text=True).stdout


def tmux_send(session: str, text: str | None = None, *keys: str) -> None:
    if text is not None:
        tmux("send-keys", "-t", session, "-l", text)
    for key in keys:
        tmux("send-keys", "-t", session, key)


async def terminal_claude(name: str, cwd: Path, *, manual: bool = False) -> dict[str, Any]:
    """`claude --model haiku` in our private tmux, trust accepted; its registry row."""
    tmux(
        "new-session",
        "-d",
        "-s",
        name,
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        str(cwd),
        "claude --model haiku",
    )

    async def ready() -> dict[str, Any] | None:
        screen = tmux_screen(name)
        if "Yes, I trust this folder" in screen:
            tmux_send(name, None, "Down")
            await asyncio.sleep(0.5)
            tmux_send(name, None, "Enter")
            await asyncio.sleep(2)
            return None
        rows = [row for row in claude_registry() if row.get("cwd") == str(cwd)]
        return rows[0] if rows and "mode on" in screen else None

    row = await until(ready, 60, 1)
    if manual:

        async def manual_mode() -> bool:
            if "manual mode on" in tmux_screen(name):
                return True
            tmux_send(name, None, "BTab")
            await asyncio.sleep(1)
            return False

        await until(manual_mode, 20, 0.5)
    return dict(row)


async def terminal_turn(name: str, cwd: Path, text: str) -> dict[str, Any]:
    """Type one prompt in the terminal; returns the registry row once idle again."""
    tmux_send(name, text, "Enter")
    await asyncio.sleep(3)

    async def idle() -> dict[str, Any] | None:
        rows = [row for row in claude_registry() if row.get("cwd") == str(cwd)]
        if rows and rows[0].get("sessionId") and rows[0].get("status") == "idle":
            found = list(
                (Path.home() / ".claude" / "projects").glob(f"*/{rows[0]['sessionId']}.jsonl")
            )
            return rows[0] if found else None
        return None

    return dict(await until(idle, 180, 1))


async def terminal_codex(name: str, cwd: Path) -> None:
    """A Codex TUI on OUR app-server (`codex --remote`), folder trusted, update skipped."""
    tmux(
        "new-session",
        "-d",
        "-s",
        name,
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        str(cwd),
        f"codex --remote unix://{codex_socket()} -C {cwd} -m gpt-6-luna "
        '-c model_reasoning_effort="low" -a on-request -s workspace-write',
    )

    async def ready() -> bool:
        screen = tmux_screen(name)
        if "Update now" in screen:
            tmux_send(name, None, "Escape")
        elif "Trust and continue" in screen:
            tmux_send(name, None, "Enter")
        elif "Ask Codex to do anything" in screen:
            return True
        await asyncio.sleep(1)
        return False

    await until(ready, 60, 1)


async def codex_tui_prompt(name: str, text: str, key: str = "Enter") -> None:
    """Type in the TUI; Enter submits (steers a running turn), Tab queues it."""
    tmux_send(name, text)
    await asyncio.sleep(0.5)
    tmux_send(name, None, key)
    await asyncio.sleep(1)
    if text[:30] in tmux_screen(name).split("›")[-1]:
        tmux_send(name, None, key)


async def loaded_threads() -> set[str]:
    (result,) = await codex_rpc([("thread/loaded/list", {})])
    return set(result.get("data") or [])


def tmux_screen(session: str) -> str:
    return tmux("capture-pane", "-p", "-t", session, "-S", "-200")


def claude_registry() -> list[dict[str, Any]]:
    """The official read-only live session list (`claude agents --json`)."""
    out = subprocess.run(
        ["claude", "agents", "--json"], capture_output=True, text=True, timeout=30
    ).stdout
    start = out.find("[")
    return list(json.loads(out[start:])) if start >= 0 else []


def journal_commands(path: Path) -> list[dict[str, Any]]:
    """The connector journal's original commands: ids and actions only, no bodies."""
    import sqlite3

    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = database.execute("SELECT id, command, receipt FROM operations").fetchall()
    finally:
        database.close()
    result = []
    for identity, command, receipt in rows:
        value = json.loads(command)
        arguments = value.get("arguments") or {}
        result.append(
            {
                "operation_id": identity,
                "action": value.get("action"),
                "instance_id": value.get("instance_id"),
                "session_id": value.get("session_id") or arguments.get("session_id"),
                "observation": arguments.get("observation") or value.get("observation"),
                "receipt_status": (json.loads(receipt) if receipt else {}).get("status"),
            }
        )
    return result


def by_local(instances: list[dict[str, Any]]) -> dict[str, str]:
    return {item["local_id"]: item["id"] for item in instances}


async def codex_rpc(calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """One short client of OUR app-server: initialize, run the calls, disconnect."""
    from websockets.asyncio.client import unix_connect

    results: list[Any] = []
    async with unix_connect(codex_socket(), uri="ws://localhost/", max_size=None) as ws:

        async def request(method: str, params: dict[str, Any]) -> Any:
            key = str(uuid4())
            await ws.send(json.dumps({"id": key, "method": method, "params": params}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == key and "method" not in message:
                    if "error" in message:
                        return {"error": message["error"]}
                    return message.get("result")

        await request(
            "initialize",
            {
                "clientInfo": {"name": "p2acc_harness", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await ws.send(json.dumps({"method": "initialized"}))
        for method, params in calls:
            results.append(await request(method, params))
    return results


def codex_items(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn/item structure of a thread: ids, types, clientIds; never text."""
    rows = []
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            rows.append(
                {
                    "turn_id": turn.get("id"),
                    "turn_status": turn.get("status"),
                    "item_id": item.get("id"),
                    "type": item.get("type"),
                    "client_id": item.get("clientId"),
                }
            )
    return rows


def record_codex_methods(monkeypatch: Any, sink: list[dict[str, Any]], started: float) -> None:
    """Every app-server method the connector's Codex driver calls (names and times only)."""
    from yuki_agent_connector.drivers import codex as codex_driver

    original = codex_driver.CodexRPC.call

    async def call_logged(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        sink.append(
            {"t": round(now() - started, 3), "method": method, "thread": params.get("threadId")}
        )
        return await original(self, method, params)

    monkeypatch.setattr(codex_driver.CodexRPC, "call", call_logged)


def allow_option(options: Any) -> str:
    """The native one-time allow option of a request (Claude 'allow', ACP allow_once id)."""
    for option in options or []:
        if isinstance(option, str) and option in {"allow", "accept"}:
            return option
        if isinstance(option, dict) and option.get("kind") == "allow_once":
            return str(option.get("optionId") or option.get("option_id"))
    return "allow"


def started(tool: dict[str, Any] | None) -> str | None:
    """The record ID of a start_subtask result (running, or refused at once)."""
    if (
        tool
        and isinstance(tool.get("id"), str)
        and ("dispatch" in tool or tool.get("status") == "failed")
    ):
        return str(tool["id"])
    return None


def run_root(name: str) -> Path:
    """The Bot's data directory for one run, kept in scratch for inspection."""
    path = SCRATCH / "runs" / f"{name}-{time.strftime('%H%M%S')}"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def tui_thread(name: str, before: set[str], cwd: Path) -> str:
    """The thread our TUI in ``cwd`` opened (new loaded thread with that cwd)."""

    async def found() -> str | None:
        for identity in await loaded_threads() - before:
            (read,) = await codex_rpc(
                [("thread/read", {"threadId": identity, "includeTurns": False})]
            )
            if ((read or {}).get("thread") or {}).get("cwd") == str(cwd):
                return identity
        lines = [line for line in tmux_screen(name).splitlines() if line.startswith("›")]
        if lines and "Ask Codex" not in lines[-1]:
            tmux_send(name, None, "Enter")  # the prompt is still in the input box
        return None

    return str(await until(found, 120, 2))


async def tui_turns_done(name: str, thread_id: str, count: int, limit: float = 180) -> None:
    """Wait until the thread has ``count`` completed turns, nudging an unsent input."""

    async def done() -> bool:
        (read,) = await codex_rpc([("thread/read", {"threadId": thread_id, "includeTurns": True})])
        turns = ((read or {}).get("thread") or {}).get("turns") or []
        if len(turns) >= count and turns[-1].get("status") == "completed":
            return True
        lines = [line for line in tmux_screen(name).splitlines() if line.startswith("›")]
        if len(turns) < count and lines and "Ask Codex" not in lines[-1]:
            tmux_send(name, None, "Enter")
        return False

    await until(done, limit, 2)


def _tool_name(tool: Any) -> Any:
    """A tool name only: ACP sends the whole toolCall (with its text); keep its kind."""
    if isinstance(tool, dict):
        return tool.get("kind") or "tool_call"
    return tool
