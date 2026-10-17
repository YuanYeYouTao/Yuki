"""ACP resources and prompt receipts in the original Manager job store."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from yuki.sandbox.acp_config import load_agents
from yuki.sandbox.acp_peer import atomic_json, operation_key, post_request, read_events, read_json
from yuki.sandbox.manager import TERMINAL, identifier

if TYPE_CHECKING:
    from yuki.sandbox.persistent import PersistentManager

ACP_KINDS = frozenset({"acp_peer", "acp_prompt", "acp_control"})


class ACPResources:
    def __init__(self, manager: PersistentManager, config: Path | None) -> None:
        self.manager = manager
        self.agents = load_agents(config)

    def row(self, identity: str) -> sqlite3.Row:
        row = self.manager.db.execute(
            "SELECT jobs.*,environment_jobs.kind,environment_jobs.dispatched "
            "FROM jobs JOIN environment_jobs USING(id) WHERE jobs.id=?",
            (identifier(identity),),
        ).fetchone()
        if row is None:
            raise ValueError("run_not_found")
        return cast(sqlite3.Row, row)

    def kind(self, identity: str) -> str | None:
        row = self.manager.db.execute(
            "SELECT kind FROM environment_jobs WHERE id=?", (identity,)
        ).fetchone()
        return row[0] if row else None

    def root(self, identity: str) -> Path:
        return self.manager.runtime_root / identifier(identity)

    def metadata(self, peer_id: str) -> dict[str, Any]:
        try:
            return read_json(self.root(peer_id) / "peer_metadata.json")
        except FileNotFoundError:
            return {}

    def _create(
        self,
        method: str,
        args: dict[str, Any],
        request_id: str,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("invalid_request_id")
        encoded = json.dumps(args, sort_keys=True)
        digest = hashlib.sha256((method + ":" + encoded).encode()).hexdigest()
        previous = self.manager.db.execute(
            "SELECT id,payload_hash FROM jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        if previous:
            if previous["payload_hash"] != digest:
                raise ValueError("idempotency_conflict")
            return previous["id"], False
        identity = str(uuid4())
        with self.manager.db:
            if kind == "acp_control" and payload is not None and payload["method"] == "close":
                peer_id = payload.get("peer_id")
                if peer_id is None:
                    resource = self.manager.db.execute(
                        "SELECT id FROM jobs WHERE request_id=?",
                        (payload["resource_request_id"],),
                    ).fetchone()
                    if resource is None:
                        peer_id = str(uuid4())
                        # No open parameters have arrived. This is the original
                        # resource's cancellation receipt, not a second task.
                        self.manager.db.execute(
                            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                            (
                                peer_id,
                                payload["resource_request_id"],
                                "",
                                "{}",
                                "cancelled",
                                json.dumps({"closing": True, "closed": True}),
                                time.time(),
                            ),
                        )
                        self.manager.db.execute(
                            "INSERT INTO environment_jobs(id,kind) VALUES (?,?)",
                            (peer_id, "acp_peer"),
                        )
                    else:
                        peer_id = resource["id"]
                if self.kind(peer_id) != "acp_peer":
                    raise ValueError("acp_peer_not_found")
                payload = {**payload, "peer_id": peer_id}
                self.manager.db.execute(
                    "UPDATE jobs SET result=json_set(result,'$.closing',json('true')) WHERE id=?",
                    (peer_id,),
                )
            self.manager.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    request_id,
                    digest,
                    json.dumps(payload if payload is not None else args),
                    "queued",
                    "{}",
                    time.time(),
                ),
            )
            self.manager.db.execute(
                "INSERT INTO environment_jobs(id,kind) VALUES (?,?)", (identity, kind)
            )
        return identity, True

    def view(self, identity: str) -> dict[str, Any]:
        row = self.row(identity)
        saved = json.loads(row["result"])
        payload = json.loads(row["payload"])
        peer_id = (
            identity if row["kind"] == "acp_peer" else saved.get("peer_id", payload.get("peer_id"))
        )
        metadata = self.metadata(peer_id) if peer_id is not None else {}
        result = {
            **saved,
            "request_id": row["request_id"],
            "kind": row["kind"],
            "run_id": identity,
            "peer_id": peer_id,
            "session_id": metadata.get("session_id", saved.get("session_id")),
            "peer_metadata": metadata,
            "status": row["state"],
            "pending": row["state"] not in TERMINAL,
            "external_untrusted": True,
        }
        if row["kind"] == "acp_peer":
            result["pending"] = not metadata.get("session_id") and row["state"] not in TERMINAL
        elif row["kind"] == "acp_prompt" and row["state"] in TERMINAL:
            result["completion"] = {
                **saved,
                "run_id": identity,
                "status": row["state"],
                "pending": False,
                "external_untrusted": True,
            }
        if row["kind"] != "acp_peer":
            result["permission_requests"] = [
                question
                for question in metadata.get("permission_requests", [])
                if question["run_id"] == identity
            ]
        return result

    def query(self, identity: str, after_cursor: int | None = None) -> dict[str, Any]:
        result = self.view(identity)
        if self.kind(identity) != "acp_prompt" or result.get("purged"):
            return result
        start = result.get("event_cursor_start", 0)
        end = result.get("event_cursor_end")
        cursor = start if after_cursor is None else after_cursor
        if cursor < start or (end is not None and cursor > end):
            raise ValueError("invalid_acp_prompt_cursor")
        page = read_events(self.root(result["peer_id"]), after_cursor=cursor)
        events = [
            event
            for event in page["events"]
            if event["run_id"] == identity and (end is None or event["cursor"] <= end)
        ]
        next_cursor = min(page["next_cursor"], end) if end is not None else page["next_cursor"]
        return {
            **result,
            "events": events,
            "next_cursor": next_cursor,
            "has_more": page["has_more"] and (end is None or next_cursor < end),
        }

    def peer_open(self, args: dict[str, Any], request_id: str) -> dict[str, Any]:
        if args.get("peer_id"):
            if self.kind(args["peer_id"]) != "acp_peer":
                raise ValueError("acp_peer_not_found")
            return self.view(args["peer_id"])
        agent_id = args["agent_id"]
        previous = self.manager.db.execute(
            "SELECT id,payload_hash FROM jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        if previous:
            if previous["payload_hash"] == "" and self.kind(previous["id"]) == "acp_peer":
                return self.view(previous["id"])
            identity, _ = self._create("acp_peer_open", args, request_id, "acp_peer")
            return self.query(identity)
        if agent_id not in self.agents:
            raise ValueError("acp_agent_not_configured")
        if not self.manager.ready:
            raise ValueError("environment_unavailable")
        identity, _ = self._create(
            "acp_peer_open", args, request_id, "acp_peer", payload=self.agents[agent_id].spec()
        )
        self.manager.queue.put_nowait(identity)
        return self.query(identity)

    def prompt_submit(self, args: dict[str, Any], request_id: str) -> dict[str, Any]:
        previous = self.manager.db.execute(
            "SELECT id FROM jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        if previous:
            identity, _ = self._create("acp_prompt_submit", args, request_id, "acp_prompt")
            return self.query(identity)
        peer = self.row(args["peer_id"])
        if peer["kind"] != "acp_peer":
            raise ValueError("acp_peer_not_found")
        if peer["state"] in TERMINAL or json.loads(peer["result"]).get("closing"):
            raise ValueError("acp_peer_closed")
        if not isinstance(args.get("prompt"), str):
            raise ValueError("invalid_acp_prompt")
        identity, _ = self._create("acp_prompt_submit", args, request_id, "acp_prompt")
        self.refresh()
        return self.query(identity)

    def _request(self, row: sqlite3.Row) -> dict[str, Any]:
        args = json.loads(row["payload"])
        method = "prompt" if row["kind"] == "acp_prompt" else args["method"]
        return {
            "version": 1,
            "operation_id": row["request_id"],
            "method": method,
            "peer_id": args["peer_id"],
            "run_id": row["id"] if method == "prompt" else args.get("run_id"),
            "payload": {
                key: value
                for key, value in args.items()
                if key not in {"peer_id", "method", "resource_request_id"}
            },
        }

    def refresh(self) -> None:
        rows = self.manager.db.execute(
            "SELECT jobs.*,environment_jobs.kind,environment_jobs.dispatched FROM jobs "
            "JOIN environment_jobs USING(id) WHERE kind IN ('acp_prompt','acp_control') "
            "AND state IN ('queued','running')"
        ).fetchall()
        for row in rows:
            args = json.loads(row["payload"])
            peer_id = args["peer_id"]
            root = self.root(peer_id)
            path = root / "rpc" / "results" / (operation_key(row["request_id"]) + ".json")
            if path.exists():
                receipt = read_json(path)
                if args.get("purge"):
                    if self.row(peer_id)["state"] not in TERMINAL:
                        continue
                    self._purge(peer_id, preserve_id=row["id"])
                    receipt = {"status": "succeeded", "closed": True, "purged": True}
                self.manager.finish(
                    row["id"],
                    receipt["status"],
                    {**receipt, "peer_id": peer_id},
                )
                continue
            peer = self.row(peer_id)
            if peer["state"] in TERMINAL:
                if args.get("method") == "close":
                    if args.get("purge"):
                        self._purge(peer_id, preserve_id=row["id"])
                    self.manager.finish(
                        row["id"],
                        "succeeded",
                        {"peer_id": peer_id, "closed": True, "purged": bool(args.get("purge"))},
                    )
                else:
                    self.manager.finish(
                        row["id"],
                        "failed",
                        {
                            "peer_id": peer_id,
                            "error": "acp_peer_exited",
                            "uncertain": bool(row["dispatched"])
                            or any(
                                (root / "rpc" / directory / path.name).exists()
                                for directory in ("accepted", "inbox")
                            ),
                        },
                    )
                continue
            if not row["dispatched"] and (
                self.metadata(peer_id).get("session_id") or args.get("method") == "close"
            ):
                post_request(root, self._request(row))
                with self.manager.db:
                    self.manager.db.execute(
                        "UPDATE environment_jobs SET dispatched=1 WHERE id=?", (row["id"],)
                    )
                    self.manager.db.execute(
                        "UPDATE jobs SET state='running' WHERE id=?", (row["id"],)
                    )

    def _purge(self, peer_id: str, *, preserve_id: str) -> None:
        session_id = self.metadata(peer_id).get("session_id")
        rows = self.manager.db.execute(
            "SELECT jobs.id,payload,result,state FROM jobs JOIN environment_jobs USING(id) "
            "WHERE kind IN ('acp_peer','acp_prompt','acp_control') AND "
            "(jobs.id=? OR json_extract(payload,'$.peer_id')=? "
            "OR json_extract(result,'$.peer_id')=?)",
            (peer_id, peer_id, peer_id),
        ).fetchall()
        with self.manager.db:
            for row in rows:
                saved = json.loads(row["result"])
                minimal = {
                    key: saved[key]
                    for key in ("session_id", "uncertain", "closing")
                    if key in saved
                }
                minimal.update(peer_id=peer_id, purged=True, closed=True)
                if session_id is not None:
                    minimal["session_id"] = session_id
                state = row["state"]
                if state not in TERMINAL and row["id"] != preserve_id:
                    state = "failed"
                    minimal.update(uncertain=True, error="acp_peer_closed")
                self.manager.db.execute(
                    "UPDATE jobs SET payload=?,result=?,state=? WHERE id=?",
                    (json.dumps({"peer_id": peer_id}), json.dumps(minimal), state, row["id"]),
                )
                self.manager.db.execute(
                    "DELETE FROM completion_outbox WHERE run_id=?", (row["id"],)
                )
        root = self.root(peer_id)
        if root.exists():
            shutil.rmtree(root)

    def control(self, method: str, args: dict[str, Any], request_id: str) -> dict[str, Any]:
        if "run_id" in args:
            prompt = self.row(args["run_id"])
            if prompt["kind"] != "acp_prompt":
                raise ValueError("acp_prompt_not_found")
            payload = json.loads(prompt["payload"])
            saved = json.loads(prompt["result"])
            peer_id = saved.get("peer_id", payload.get("peer_id"))
            if args.get("peer_id", peer_id) != peer_id:
                raise ValueError("acp_prompt_peer_mismatch")
            args = {**args, "peer_id": peer_id}
            if method == "cancel" and prompt["state"] in TERMINAL:
                return self.query(prompt["id"])
        if method != "close" and self.kind(args["peer_id"]) != "acp_peer":
            raise ValueError("acp_peer_not_found")
        identity, _ = self._create(
            "acp_" + method,
            args,
            request_id,
            "acp_control",
            payload={**args, "method": method},
        )
        if method == "close":
            peer_id = json.loads(self.row(identity)["payload"])["peer_id"]
            peer = self.row(peer_id)
            if not peer["dispatched"] and peer["state"] not in TERMINAL:
                self.manager.finish(peer["id"], "cancelled", {"closing": True, "closed": True})
        self.refresh()
        result = self.view(identity)
        if method == "close":
            return {**result, "operation_id": request_id, "admitted": True, "closing": True}
        if method == "cancel":
            return {
                **self.query(args["run_id"]),
                "operation_id": request_id,
                "cancel_requested": True,
            }
        return result

    def events(self, args: dict[str, Any]) -> dict[str, Any]:
        peer_id = identifier(args["peer_id"])
        peer = self.view(peer_id)
        if self.kind(peer_id) != "acp_peer":
            raise ValueError("acp_peer_not_found")
        if peer.get("purged"):
            return {
                "peer_id": peer_id,
                "events": [],
                "next_cursor": 0,
                "has_more": False,
                "purged": True,
            }
        root = self.root(peer_id)
        acknowledged = root / "rpc" / "events.ack"
        previous = read_json(acknowledged)["cursor"] if acknowledged.exists() else 0
        cursor = args.get("ack_cursor")
        if cursor is not None and cursor > previous:
            path = root / "rpc" / "events.jsonl"
            with path.open("rb") as stream:
                if cursor > path.stat().st_size:
                    raise ValueError("invalid_acp_event_ack")
                stream.seek(cursor - 1)
                if stream.read(1) != b"\n":
                    raise ValueError("invalid_acp_event_ack")
            atomic_json(acknowledged, {"cursor": cursor})
            previous = cursor
        page = read_events(root, after_cursor=max(previous, args.get("after_cursor", 0)))
        return {"peer_id": peer_id, **page}

    async def handle(self, method: str, args: dict[str, Any], request_id: str) -> dict[str, Any]:
        if method == "acp_agents":
            return {
                "agents": [
                    {"id": agent.id, "description": agent.description}
                    for agent in self.agents.values()
                ]
            }
        if method == "acp_peer_open":
            return self.peer_open(args, request_id)
        if method == "acp_prompt_submit":
            return self.prompt_submit(args, request_id)
        if method == "acp_peer_events":
            return self.events(args)
        if method in {"acp_permission_reply", "acp_prompt_cancel", "acp_peer_close"}:
            operation = {
                "acp_permission_reply": "permission_reply",
                "acp_prompt_cancel": "cancel",
                "acp_peer_close": "close",
            }[method]
            return self.control(operation, args, request_id)
        if method == "acp_prompt_query":
            self.refresh()
            identity = args.get("run_id", args.get("peer_id"))
            if identity is None:
                row = self.manager.db.execute(
                    "SELECT id FROM jobs WHERE request_id=?", (args["request_id"],)
                ).fetchone()
                if row is None:
                    return {"error": "unknown_request"}
                identity = row["id"]
            return self.query(identity, args.get("after_cursor"))
        raise ValueError("unknown_method")
