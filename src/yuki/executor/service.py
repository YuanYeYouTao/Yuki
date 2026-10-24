"""Actual executor connections and durable, person-scoped local operations."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import (
    ClientIdentityService,
    ClientPrincipal,
    fingerprint,
)
from yuki.client_api.pagination import cursor, decode_cursor
from yuki.executor.db_models import ExecutorGrantModel, ExecutorModel, ExecutorOperationModel
from yuki.identity.credentials import secret_hash
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import current_person_id, person_family_condition, same_person_id
from yuki.time.formatting import utc_iso
from yuki.workspace.store import WorkspaceError, WorkspaceStore

TERMINAL = frozenset({"succeeded", "failed", "unknown"})


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def operation_view(row: ExecutorOperationModel) -> dict[str, Any]:
    return {
        "op_id": row.op_id,
        "operation_id": row.op_id,
        "capability": row.capability,
        "params": json.loads(row.params_json or "{}"),
        "state": row.state,
        "level": row.level,
        "result": json.loads(row.result_json or "{}"),
        "observation": json.loads(row.observation_json) if row.observation_json else None,
        "created_at": utc_iso(row.created_at),
        "dispatched_at": utc_iso(row.dispatched_at),
        "acknowledged_at": utc_iso(row.acknowledged_at),
        "settled_at": utc_iso(row.settled_at),
    }


def operation_evidence(row: ExecutorOperationModel) -> dict[str, Any]:
    return {
        "tool": "local_device",
        "op_id": row.op_id,
        "operation_id": row.op_id,
        "state": row.state,
        "status": row.state,
        "level": row.level,
        "observation": json.loads(row.observation_json) if row.observation_json else None,
        "pending": row.state not in TERMINAL,
        "ok": row.state != "failed",
        "uncertain": row.state == "unknown",
    }


class LocalExecutors:
    def __init__(
        self, identity: ClientIdentityService, store: WorkspaceStore | None = None
    ) -> None:
        self.identity, self.database, self.store = identity, identity.database, store
        self.connections: dict[str, int] = {}
        self.local_paused: dict[str, bool] = {}
        self.dispatching: set[str] = set()

    async def _executor(self, session: AsyncSession, identity: str) -> ExecutorModel:
        row = await session.get(ExecutorModel, identity)
        if row is None or row.revoked_at is not None:
            raise ClientError("unauthenticated")
        person = await session.get(CanonicalPersonModel, row.person_id)
        root = await session.get(
            CanonicalPersonModel, await current_person_id(session, row.person_id)
        )
        if person is None or not person.enabled or root is None or not root.enabled:
            raise ClientError("unauthenticated")
        return row

    async def authenticate(self, token: str) -> str:
        async with self.database.read_session() as session:
            identity = await session.scalar(
                select(ExecutorModel.id).where(ExecutorModel.token_hash == secret_hash(token))
            )
            if identity is None:
                raise ClientError("unauthenticated")
            return (await self._executor(session, identity)).id

    async def _owned(self, session: AsyncSession, person: str, identity: str) -> ExecutorModel:
        row = await session.get(ExecutorModel, identity)
        if (
            row is None
            or row.revoked_at is not None
            or not await same_person_id(session, person, row.person_id)
        ):
            raise ClientError("not_found")
        return row

    async def grants(self, session: AsyncSession, identity: str) -> list[dict[str, Any]]:
        rows = (
            await session.scalars(
                select(ExecutorGrantModel)
                .where(ExecutorGrantModel.executor_id == identity)
                .order_by(ExecutorGrantModel.capability, ExecutorGrantModel.scope_alias)
            )
        ).all()
        return [
            {
                "capability": row.capability,
                **(
                    {"scope": {"alias": row.scope_alias, "digest": row.scope_digest}}
                    if row.scope_alias
                    else {}
                ),
            }
            for row in rows
        ]

    async def _config(self, session: AsyncSession, row: ExecutorModel) -> dict[str, Any]:
        grants = await self.grants(session, row.id)
        return {
            "executor_id": row.id,
            "paused": row.paused,
            "revision": row.revision,
            "grants": grants,
            "grants_digest": secret_hash(canonical(grants)),
        }

    async def config(self, identity: str) -> dict[str, Any]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            return await self._config(session, await self._executor(session, identity))

    async def _list(self, session: AsyncSession, person: str) -> list[dict[str, Any]]:
        rows = (
            await session.scalars(
                select(ExecutorModel)
                .where(
                    person_family_condition(ExecutorModel.person_id, person),
                    ExecutorModel.revoked_at.is_(None),
                )
                .order_by(ExecutorModel.created_at, ExecutorModel.id)
            )
        ).all()
        return [
            {
                **await self._config(session, row),
                "label": row.label,
                "platform": row.platform,
                "online": bool(self.connections.get(row.id)),
                "last_seen_at": utc_iso(row.last_seen_at),
                "claimed": row.last_seen_at is not None,
            }
            for row in rows
        ]

    async def list(self, principal: ClientPrincipal) -> list[dict[str, Any]]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            return await self._list(session, principal.person_id)

    async def command(
        self,
        principal: ClientPrincipal,
        request_id: str,
        action: str,
        identity: str = "",
        **payload: Any,
    ) -> dict[str, Any]:
        operation = f"client.executor.{action}"
        bound = fingerprint(operation, identity, payload)
        raw = secrets.token_urlsafe(32) if action == "create" else None
        async with self.database.immediate_session() as session:
            principal = await self.identity.require_principal(session, principal)
            previous = await self.identity.replay(
                session, principal.device_id, request_id, bound, principal=principal
            )
            if previous is not None:
                return previous  # Creation replays deliberately contain no secret.
            now = datetime.now(UTC)
            if action == "create":
                identity = str(uuid4())
                assert raw is not None
                row = ExecutorModel(
                    id=identity,
                    person_id=principal.source_person_id,
                    token_hash=secret_hash(raw),
                    label=payload["label"],
                    platform=payload["platform"],
                    created_at=now,
                    revision=1,
                    paused=False,
                )
                session.add(row)
                response: dict[str, Any] = {
                    "executor_id": identity,
                    "label": row.label,
                    "platform": row.platform,
                    "created_at": utc_iso(now),
                }
            else:
                row = await self._owned(session, principal.person_id, identity)
                if action == "grants":
                    if row.revision != payload["expected_revision"]:
                        raise ClientError(
                            "version_conflict",
                            details={"current": await self._config(session, row)},
                        )
                    await session.execute(
                        delete(ExecutorGrantModel).where(ExecutorGrantModel.executor_id == identity)
                    )
                    for grant in payload["grants"]:
                        scope = grant.get("scope") or {}
                        session.add(
                            ExecutorGrantModel(
                                executor_id=identity,
                                capability=grant["capability"],
                                scope_alias=scope.get("alias", ""),
                                scope_digest=scope.get("digest", ""),
                                revision=row.revision + 1,
                            )
                        )
                elif action in {"pause", "resume"}:
                    row.paused = action == "pause"
                elif action == "delete":
                    row.revoked_at = now
                else:
                    raise ValueError("unknown executor command")
                row.revision += 1
                await session.flush()
                response = await self._config(session, row)
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=principal.device_id,
                principal_kind="device",
                person_id=principal.person_id,
                request_id=request_id,
                operation=operation,
                operation_ref=identity,
                payload_hash=bound,
                resource_id=identity,
                result=response,
                request_person_id=principal.request_person_id,
                request_conversation_id=principal.request_conversation_id,
            )
        return {**response, **({"token": raw} if raw else {}), "replay": False}

    async def heartbeat(self, identity: str, local_paused: bool | None) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            row = await self._executor(session, identity)
            if row.last_seen_at is None:
                row.revision += 1
            row.last_seen_at = datetime.now(UTC)
            config = await self._config(session, row)
        if local_paused is not None:
            self.local_paused[identity] = local_paused
        return {"grants_digest": config["grants_digest"], "paused": config["paused"]}

    async def _op(self, session: AsyncSession, identity: str, op_id: str) -> ExecutorOperationModel:
        await self._executor(session, identity)
        row = await session.get(ExecutorOperationModel, op_id)
        if row is None or row.executor_id != identity:
            raise ClientError("not_found")
        return row

    async def get(self, identity: str, op_id: str) -> dict[str, Any]:
        async with self.database.read_session() as session:
            return operation_view(await self._op(session, identity, op_id))

    async def ack(self, identity: str, op_id: str) -> dict[str, Any]:
        async with self.database.immediate_session() as session:
            row = await self._op(session, identity, op_id)
            if row.state == "dispatched":
                row.state, row.acknowledged_at = "acknowledged", datetime.now(UTC)
            elif row.state == "registered":
                raise ClientError("precondition_failed")
            return operation_view(row)

    async def result(self, identity: str, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
        state, level = body["state"], body.get("level")
        result, observation = canonical(body.get("result", {})), body.get("observation")
        observed = canonical(observation) if observation is not None else None
        from yuki.executor.parameters import validate_result

        result_problem: ClientError | None = None
        async with self.database.read_session() as reader:
            original = await self._op(reader, identity, op_id)
            try:
                validate_result(
                    original.capability,
                    json.loads(original.params_json or "{}"),
                    state,
                    body.get("result", {}),
                )
            except ClientError as exc:
                result_problem = exc
            receipt_hash = secret_hash(
                canonical([original.receipt_hash, state, level, result, observed])
            )
        async with self.database.immediate_session() as session:
            row = await self._op(session, identity, op_id)
            if row.state in TERMINAL:
                if (row.state, row.level, row.result_json, row.observation_json) != (
                    state,
                    level,
                    result,
                    observed,
                ):
                    raise ClientError("request_conflict")
                return operation_view(row)
            if result_problem is not None:
                raise result_problem
            if row.state not in {"acknowledged", "awaiting_user"}:
                raise ClientError("precondition_failed")
            expected = {
                "folder.read": "confirmed",
                "folder.write": "confirmed",
                "open.url": "submitted",
                "open.file": "submitted",
                "notify": "submitted",
            }
            if (
                state == "succeeded"
                and row.capability in expected
                and level != expected[row.capability]
            ):
                raise ClientError("validation_error")
            if state != "succeeded" and level is not None:
                raise ClientError("validation_error")
            if state == "unknown" and body.get("result"):
                raise ClientError("validation_error")
            row.state, row.level, row.result_json, row.observation_json = (
                state,
                level,
                result,
                observed,
            )
            if state in TERMINAL:
                row.settled_at = datetime.now(UTC)
                row.receipt_hash = receipt_hash
            view = operation_view(row)
        if state in TERMINAL:
            self.dispatching.discard(op_id)
        return view

    async def content(self, identity: str, op_id: str) -> tuple[dict[str, Any], bytes]:
        async with self.database.read_session() as session:
            row = await self._op(session, identity, op_id)
            params = json.loads(row.params_json or "{}")
            if (
                row.state in TERMINAL
                or row.capability != "folder.write"
                or not params.get("artifact_id")
            ):
                raise ClientError("not_found")
        if self.store is None:
            raise ClientError("not_found")
        try:
            metadata, data = await asyncio.to_thread(self.store.read_bytes, params["artifact_id"])
        except (WorkspaceError, OSError):
            raise ClientError("not_found") from None
        if not metadata["immutable"] or metadata["sha256"] != params.get("version"):
            raise ClientError("not_found")
        async with self.database.read_session() as session:
            current = await self._op(session, identity, op_id)
            if current.state in TERMINAL:
                raise ClientError("not_found")
        return metadata, data

    async def history(
        self, principal: ClientPrincipal, identity: str, before: str | None, limit: int
    ) -> dict[str, Any]:
        position = decode_cursor(before) if before else None
        if position is not None:
            if (
                not isinstance(position, list)
                or len(position) != 2
                or any(not isinstance(value, str) for value in position)
            ):
                raise ClientError("validation_error")
            try:
                position = [datetime.fromisoformat(position[0]), position[1]]
            except (ValueError, TypeError, IndexError):
                raise ClientError("validation_error") from None
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            await self._owned(session, principal.person_id, identity)
            query = select(ExecutorOperationModel).where(
                ExecutorOperationModel.executor_id == identity
            )
            if position is not None:
                query = query.where(
                    tuple_(ExecutorOperationModel.created_at, ExecutorOperationModel.op_id)
                    < tuple(position)
                )
            rows = (
                await session.scalars(
                    query.order_by(
                        ExecutorOperationModel.created_at.desc(),
                        ExecutorOperationModel.op_id.desc(),
                    ).limit(limit + 1)
                )
            ).all()
            return {
                "items": [operation_view(row) for row in rows[:limit]],
                "next_cursor": cursor([utc_iso(rows[limit - 1].created_at), rows[limit - 1].op_id])
                if len(rows) > limit
                else None,
            }

    async def stream(self, identity: str) -> AsyncIterator[str]:
        queue = self.database.commit_hints.subscribe()
        self.connections[identity] = self.connections.get(identity, 0) + 1
        try:
            async with self.database.immediate_session() as session:
                row = await self._executor(session, identity)
                row.revision += 1
            await self.heartbeat(identity, None)
            config = await self.config(identity)
            async with self.database.read_session() as session:
                pending = list(
                    await session.scalars(
                        select(ExecutorOperationModel.op_id).where(
                            ExecutorOperationModel.executor_id == identity,
                            ExecutorOperationModel.op_id.in_(self.dispatching),
                            ExecutorOperationModel.state.not_in(TERMINAL),
                        )
                    )
                )
            yield (
                "event: sync\ndata: "
                + canonical({"pending": pending, "grants_digest": config["grants_digest"]})
                + "\n\n"
            )
            while True:
                try:
                    available = await asyncio.wait_for(queue.get(), 15)
                except TimeoutError:
                    await self.config(identity)  # Revocation is checked even on a quiet connection.
                    yield ": keepalive\n\n"
                    continue
                if available is False:
                    return
                current = await self.config(identity)
                if (
                    current["grants_digest"] != config["grants_digest"]
                    or current["revision"] != config["revision"]
                ):
                    yield (
                        "event: config\ndata: "
                        + canonical({"grants_digest": current["grants_digest"]})
                        + "\n\n"
                    )
                    config = current
                async with self.database.read_session() as session:
                    pending = list(
                        await session.scalars(
                            select(ExecutorOperationModel.op_id).where(
                                ExecutorOperationModel.executor_id == identity,
                                ExecutorOperationModel.op_id.in_(self.dispatching),
                                ExecutorOperationModel.state.not_in(TERMINAL),
                            )
                        )
                    )
                for op_id in pending:
                    yield "event: operation\ndata: " + canonical({"op_id": op_id}) + "\n\n"
        except ClientError:
            return
        finally:
            self.database.commit_hints.unsubscribe(queue)
            count = self.connections[identity] - 1
            if count:
                self.connections[identity] = count
            else:
                self.connections.pop(identity)
                self.local_paused.pop(identity, None)
            async with self.database.immediate_session() as session:
                disconnected = await session.get(ExecutorModel, identity)
                if disconnected is not None:
                    disconnected.revision += 1

    async def _requester(self, session: AsyncSession, person: str | None) -> str | None:
        if not person:
            return None
        original = await session.get(CanonicalPersonModel, person)
        root = await session.get(CanonicalPersonModel, await current_person_id(session, person))
        return person if original and original.enabled and root and root.enabled else None

    async def tools(
        self,
        person: str | None,
        action: str,
        arguments: dict[str, Any],
        tool_call_key: str,
        work_id: str | None,
    ) -> dict[str, Any]:
        async with self.database.read_session() as session:
            person = await self._requester(session, person)
            if not person:
                if action == "list":
                    return {"items": []}
                raise ClientError("not_found")
            if action == "list":
                return {"items": await self._list(session, person)}
            if action == "get":
                row = await session.get(ExecutorOperationModel, arguments.get("op_id"))
                if (
                    row is None
                    or row.person_id is None
                    or not await same_person_id(session, row.person_id, person)
                ):
                    raise ClientError("not_found")
                return {**operation_view(row), **operation_evidence(row)}
        if action != "execute":
            raise ClientError("validation_error")
        capability, params = arguments.get("capability"), arguments.get("params", {})
        if not isinstance(capability, str) or not capability or not isinstance(params, dict):
            raise ClientError("validation_error")
        from yuki.executor.parameters import validate_parameters

        validate_parameters(capability, params)
        params_json = canonical(params)
        async with self.database.read_session() as session:
            prior = await session.scalar(
                select(ExecutorOperationModel).where(
                    ExecutorOperationModel.tool_call_key == tool_call_key
                )
            )
            if prior is not None:
                if (
                    prior.person_id != person
                    or prior.work_id != work_id
                    or prior.executor_id != arguments.get("executor_id")
                    or prior.capability != capability
                    or prior.params_json != params_json
                ):
                    raise ClientError("request_conflict")
                return {**operation_view(prior), **operation_evidence(prior)}
        if prior is None and capability == "folder.write" and params.get("artifact_id"):
            if self.store is None:
                raise ClientError("not_found")
            try:
                artifact, _ = await asyncio.to_thread(self.store.read_bytes, params["artifact_id"])
            except (WorkspaceError, OSError):
                raise ClientError("not_found") from None
            if not artifact["immutable"] or artifact["sha256"] != params.get("version"):
                raise ClientError("not_found")
        async with self.database.immediate_session() as session:
            if not await self._requester(session, person):
                raise ClientError("not_found")
            row = await session.scalar(
                select(ExecutorOperationModel).where(
                    ExecutorOperationModel.tool_call_key == tool_call_key
                )
            )
            if row is not None:
                if (
                    row.person_id != person
                    or row.executor_id != arguments.get("executor_id")
                    or row.capability != capability
                    or row.params_json != params_json
                    or row.work_id != work_id
                ):
                    raise ClientError("request_conflict")
                return {**operation_view(row), **operation_evidence(row)}
            else:
                executor = await self._owned(session, person, arguments.get("executor_id", ""))
                if executor.paused or self.local_paused.get(executor.id):
                    raise ClientError("executor_paused")
                if not self.connections.get(executor.id):
                    raise ClientError("executor_offline")
                grants = await self.grants(session, executor.id)
                path = params.get("path", "")
                if not any(
                    g["capability"] == capability
                    and (
                        "scope" not in g
                        or (
                            isinstance(path, str)
                            and (
                                path == g["scope"]["alias"]
                                or path.startswith(g["scope"]["alias"] + "/")
                            )
                        )
                    )
                    for g in grants
                ):
                    raise ClientError("permission_denied")
                now = datetime.now(UTC)
                row = ExecutorOperationModel(
                    op_id=str(uuid4()),
                    executor_id=executor.id,
                    person_id=person,
                    work_id=work_id,
                    tool_call_key=tool_call_key,
                    capability=capability,
                    params_json=params_json,
                    state="dispatched",
                    receipt_hash=secret_hash(params_json),
                    created_at=now,
                    dispatched_at=now,
                )
                session.add(row)
                if work_id is not None:
                    from yuki.runtime.work_activation import current_work_control
                    from yuki.runtime.work_repository import WorkRepository

                    control = current_work_control.get()
                    if control is not None:
                        await control.repository._assert_lease(session, control.lease)
                    await WorkRepository.bind_domain_receipt(
                        session, work_id, tool_call_key, "local_device:" + row.op_id
                    )
            self.dispatching.add(row.op_id)
        return {**operation_view(row), **operation_evidence(row)}

    async def original_operation(
        self, operation_key: str, work_id: str | None
    ) -> dict[str, Any] | None:
        """Read the original receipt for Host recovery, without granting a new dispatch."""
        async with self.database.read_session() as reader:
            row = await reader.scalar(
                select(ExecutorOperationModel).where(
                    ExecutorOperationModel.tool_call_key == operation_key,
                    ExecutorOperationModel.work_id == work_id,
                )
            )
            return {**operation_view(row), **operation_evidence(row)} if row else None
