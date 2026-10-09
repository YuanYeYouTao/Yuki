"""Worker admission, independent leases and durable parent/child mail."""

from __future__ import annotations

import builtins
import json
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.sql import Select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.runtime.subagent_schema import children, media, media_refs
from qq_ai_bot.runtime.work_repository import (
    TERMINAL,
    WorkConflict,
    WorkLease,
    WorkRepository,
    encode_json,
)
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work


class SubagentRepository:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository
        self.database = repository.database
        self.max_concurrency = int(getattr(self.database, "subagent_concurrency", 1))

    async def reopen_parent(
        self, lease: WorkLease, identity: str, *, models: int
    ) -> dict[str, Any]:
        """Resume a retained completed tree in its original scope and budget."""
        if lease.work_id:
            raise ValueError("subagent_parent_control_only")
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            child = (
                (await session.execute(select(children).where(children.c.work_id == identity)))
                .mappings()
                .first()
            )
            if child is None or child["archived_at"] is not None:
                raise ValueError("subagent_archived_or_unknown")
            root = (
                (await session.execute(select(work).where(work.c.id == child["root_id"])))
                .mappings()
                .one()
            )
            child_state = await session.scalar(select(work.c.state).where(work.c.id == identity))
            if child_state in {"cancelled", "failed"}:
                raise ValueError("subagent_not_resumable")
            if (
                root["conversation_id"] != lease.conversation_id
                or root["generation"] != lease.generation
                or root["state"] != "completed"
            ):
                raise ValueError("subagent_parent_not_resumable")
            from qq_ai_bot.runtime.work_budget import charge

            await charge(session, root["id"], models=models, tools=0)
            return dict(
                (
                    await session.execute(
                        update(work)
                        .where(work.c.id == root["id"])
                        .values(
                            state="running",
                            revision=work.c.revision + 1,
                            updated=time.time(),
                            model_requests=work.c.model_requests + models,
                        )
                        .returning(work)
                    )
                )
                .mappings()
                .one()
            )

    async def start(self, lease: WorkLease, root_id: str, key: str, brief: dict[str, Any]) -> str:
        if lease.work_id:
            raise ValueError("recursive_subagent_forbidden")
        goal = brief.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("invalid_subagent_goal")
        if brief.get("output_kind") not in {"answer", "artifact", "state_change"}:
            raise ValueError("invalid_subagent_output_kind")
        encoded = encode_json(brief)
        async with self.database.sessions() as reader:
            snapshot = (
                (await reader.execute(select(work).where(work.c.id == root_id))).mappings().one()
            )
        identity, now = str(uuid4()), time.time()
        source = json.loads(snapshot["source_json"])
        source.update(work_id=identity, parent_work_id=root_id, worker=True)
        encoded_source = encode_json(source)
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            root = (
                (await session.execute(select(work).where(work.c.id == root_id))).mappings().one()
            )
            if (
                root["conversation_id"] != lease.conversation_id
                or root["generation"] != lease.generation
                or root["state"] in TERMINAL
                or root["revision"] != snapshot["revision"]
                or root["source_json"] != snapshot["source_json"]
            ):
                raise WorkConflict("subagent_parent_unavailable")
            previous = (
                (await session.execute(select(children).where(children.c.source_key == key)))
                .mappings()
                .first()
            )
            if previous:
                if previous["root_id"] != root_id or previous["brief_json"] != encoded:
                    raise WorkConflict("subagent_start_conflict")
                return str(previous["work_id"])
            await session.execute(
                insert(work).values(
                    id=identity,
                    conversation_id=root["conversation_id"],
                    generation=root["generation"],
                    source_key=f"worker:{identity}",
                    source_json=encoded_source,
                    goal=goal,
                    output_kind=brief.get("output_kind", "answer"),
                    deliver_artifacts=False,
                    state="queued",
                    created=now,
                    updated=now,
                )
            )
            await session.execute(
                insert(children).values(
                    work_id=identity, root_id=root_id, source_key=key, brief_json=encoded
                )
            )
            return identity

    async def acquire(self, identity: str, *, reconcile: bool = False) -> WorkLease | None:
        owner = str(uuid4())
        async with self.database.immediate_session() as session:
            now = time.time()
            row = (
                (await session.execute(select(work).where(work.c.id == identity)))
                .mappings()
                .first()
            )
            states = {"completed", "failed", "suspended"} if reconcile else {"queued", "running"}
            if row is None or row["state"] not in states:
                return None
            actual = await session.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.id == row["conversation_id"]
                )
            )
            root_id = await session.scalar(
                select(children.c.root_id).where(children.c.work_id == identity)
            )
            root_state = await session.scalar(select(work.c.state).where(work.c.id == root_id))
            if actual != row["generation"] or root_state in TERMINAL or root_state is None:
                if reconcile:
                    return None
                await session.execute(
                    update(work)
                    .where(work.c.id == identity)
                    .values(
                        state="cancelled",
                        reason="parent_obsolete",
                        checkpoint_json=func.json_remove(
                            work.c.checkpoint_json, "$.accepted_control"
                        ),
                        updated=now,
                    )
                )
                return None
            active = await session.scalar(
                select(func.count()).select_from(children).where(children.c.lease_until > now)
            )
            if int(active or 0) >= self.max_concurrency:
                return None
            claimed = (
                (
                    await session.execute(
                        update(children)
                        .where(
                            children.c.work_id == identity,
                            children.c.lease_until <= now,
                            children.c.archived_at.is_(None),
                        )
                        .values(owner=owner, lease_until=now + 60, fence=children.c.fence + 1)
                        .returning(children)
                    )
                )
                .mappings()
                .first()
            )
            if claimed is None:
                return None
            if not reconcile:
                await session.execute(
                    update(work).where(work.c.id == identity).values(state="running", updated=now)
                )
            return WorkLease(
                row["conversation_id"],
                row["generation"],
                claimed["cancel_epoch"],
                claimed["fence"],
                owner,
                identity,
            )

    async def maintain(self) -> None:
        """Repair a crash after settlement, then archive only expired root trees."""
        async with self.database.sessions() as session:
            missed = list(
                await session.scalars(
                    select(children.c.work_id)
                    .join(work, work.c.id == children.c.work_id)
                    .where(
                        work.c.state.in_(("completed", "failed", "suspended")),
                        children.c.notified_revision < work.c.revision,
                        children.c.archived_at.is_(None),
                    )
                    .limit(8)
                )
            )
        for identity in missed:
            lease = await self.acquire(identity, reconcile=True)
            if lease:
                try:
                    await self.finish(lease)
                finally:
                    await self.repository.release(lease)
        now = time.time()
        cutoff = now - 7 * 86400

        def expired_query() -> Select[tuple[str]]:
            return (
                select(children.c.work_id)
                .where(
                    children.c.archived_at.is_(None),
                    children.c.lease_until <= now,
                    children.c.root_id.in_(
                        select(work.c.id).where(work.c.state.in_(TERMINAL), work.c.updated < cutoff)
                    ),
                )
                .limit(16)
            )

        # Most passes have nothing to archive. Scan before reserving SQLite's
        # single writer, then check eligibility again after acquiring it.
        async with self.database.sessions() as session:
            candidates = list(await session.scalars(expired_query()))
        if not candidates:
            return
        async with self.database.immediate_session() as session:
            expired = list(
                await session.scalars(expired_query().where(children.c.work_id.in_(candidates)))
            )
            if not expired:
                return
            # Result receipts retain summaries, artifact references and per-worker accounting.
            await session.execute(
                update(children)
                .where(children.c.work_id.in_(expired))
                .values(
                    archived_at=time.time(),
                    brief_json="{}",
                    owner=None,
                    lease_until=0,
                )
            )
            await session.execute(
                update(work)
                .where(work.c.id.in_(expired))
                .values(checkpoint_json="{}", source_json="{}")
            )
            expired_roots = list(
                await session.scalars(
                    select(work.c.id).where(
                        work.c.id.in_(
                            select(children.c.root_id).where(children.c.work_id.in_(expired))
                        ),
                        work.c.id.not_in(
                            select(children.c.root_id).where(children.c.archived_at.is_(None))
                        ),
                        work.c.state.in_(TERMINAL),
                        work.c.updated < cutoff,
                    )
                )
            )
            archived_work_ids = [*expired, *expired_roots]
            from qq_ai_bot.runtime.protocol_schema import refs as protocol_refs

            await session.execute(
                delete(protocol_refs).where(protocol_refs.c.work_id.in_(archived_work_ids))
            )
            media_hashes = list(
                await session.scalars(
                    select(media_refs.c.sha256)
                    .where(media_refs.c.work_id.in_(archived_work_ids))
                    .distinct()
                )
            )
            for table in (journal, effects, inputs, media_refs):
                await session.execute(delete(table).where(table.c.work_id.in_(archived_work_ids)))
            if media_hashes:
                await session.execute(
                    delete(media).where(
                        media.c.sha256.in_(media_hashes),
                        media.c.sha256.not_in(select(media_refs.c.sha256)),
                    )
                )

    async def related(
        self, root_id: str, identity: str, *, include_checkpoint: bool = False
    ) -> dict[str, Any]:
        async with self.database.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(
                            children,
                            work.c.state,
                            work.c.goal,
                            work.c.model_requests,
                            work.c.tool_calls,
                            *(
                                (work.c.revision, work.c.checkpoint_json)
                                if include_checkpoint
                                else ()
                            ),
                        )
                        .join(work, children.c.work_id == work.c.id)
                        .where(children.c.root_id == root_id, children.c.work_id == identity)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("subagent_not_owned")
            result = dict(row)
            if not include_checkpoint:
                return result
            receipt = json.loads(row["result_json"])
            if receipt and row["archived_at"] is None:
                # Keep the checkpoint in its original Work, not a second bounded
                # result blob. Only the notified version describes this result;
                # resumed work must not relabel a newer checkpoint as its old one.
                if row["notified_revision"] == row["revision"]:
                    receipt["checkpoint"] = json.loads(row["checkpoint_json"])
                    receipt["checkpoint_revision"] = row["revision"]
                else:
                    receipt.pop("checkpoint", None)
                    receipt["checkpoint_status"] = "unavailable_for_result_revision"
                result["result_json"] = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
            result.pop("checkpoint_json", None)
            return result

    async def list(
        self, root_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        """Internal consumers read all owned children; model directory is explicitly paged."""
        result: list[dict[str, Any]] = []
        async with self.database.sessions() as session:
            while True:
                size = limit if limit is not None else 50
                query = (
                    select(
                        children,
                        work.c.state,
                        work.c.goal,
                        work.c.model_requests,
                        work.c.tool_calls,
                    )
                    .join(work, children.c.work_id == work.c.id)
                    .where(children.c.root_id == root_id)
                )
                if cursor is not None:
                    query = query.where(children.c.work_id > cursor)
                page = list(
                    (
                        await session.execute(query.order_by(children.c.work_id).limit(size))
                    ).mappings()
                )
                result.extend(dict(row) for row in page)
                if limit is not None or len(page) < size:
                    return result
                cursor = str(page[-1]["work_id"])

    async def unfinished(self, root_id: str) -> builtins.list[dict[str, Any]]:
        """Exact nonterminal set; unlike directory pagination this is a completion fence."""
        async with self.database.sessions() as session:
            return [
                dict(row)
                for row in (
                    await session.execute(
                        select(children, work.c.state)
                        .join(work, work.c.id == children.c.work_id)
                        .where(children.c.root_id == root_id, work.c.state.not_in(TERMINAL))
                    )
                ).mappings()
            ]

    async def message(
        self,
        lease: WorkLease,
        root_id: str,
        identity: str,
        key: str,
        text: str,
        *,
        ask: bool = False,
        reply_to: str | None = None,
    ) -> str:
        if not text.strip():
            raise ValueError("invalid_subagent_message")
        if lease.work_id and lease.work_id != identity:
            raise ValueError("subagent_not_owned")
        await self.related(root_id, identity)
        destination = root_id if lease.work_id else identity
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            child = (
                (await session.execute(select(children).where(children.c.work_id == identity)))
                .mappings()
                .one()
            )
            if child["archived_at"] is not None:
                raise ValueError("subagent_archived")
            target = (
                (await session.execute(select(work).where(work.c.id == destination)))
                .mappings()
                .one()
            )
            if target["state"] in {"failed", "cancelled"} or (
                lease.work_id and target["state"] == "completed"
            ):
                raise ValueError("subagent_message_target_terminal")
            payload = {
                "text": encode_json(
                    {
                        "kind": "subagent_question" if ask else "subagent_message",
                        "child_id": identity,
                        "message_id": key,
                        "reply_to": reply_to,
                        "text": text,
                    }
                ),
                "child_id": identity,
            }
            inserted = (
                await session.execute(
                    insert(inputs)
                    .values(
                        conversation_id=lease.conversation_id,
                        generation=lease.generation,
                        source_key=key,
                        work_id=destination,
                        kind="subagent",
                        ready=True,
                        payload_json=encode_json(payload),
                        created=time.time(),
                    )
                    .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
                    .returning(inputs.c.id)
                )
            ).scalar_one_or_none()
            if inserted is not None and target["state"] != "running":
                await session.execute(
                    update(work)
                    .where(work.c.id == destination)
                    .values(state="queued", revision=work.c.revision + 1, updated=time.time())
                )
            return key

    async def cancel(self, lease: WorkLease, root_id: str, identity: str) -> None:
        await self.related(root_id, identity)
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            await session.execute(
                update(children)
                .where(children.c.work_id == identity)
                .values(owner=None, lease_until=0, cancel_epoch=children.c.cancel_epoch + 1)
            )
            await session.execute(
                update(work)
                .where(work.c.id == identity)
                .values(
                    state="cancelled",
                    reason="parent_cancelled",
                    checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.accepted_control"),
                    revision=work.c.revision + 1,
                    updated=time.time(),
                )
            )

    async def finish(self, lease: WorkLease) -> None:
        """Notify the parent with the committed result of this exact revision."""
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            child = (
                (await session.execute(select(children).where(children.c.work_id == lease.work_id)))
                .mappings()
                .one()
            )
            row = (
                (await session.execute(select(work).where(work.c.id == lease.work_id)))
                .mappings()
                .one()
            )
            checkpoint = json.loads(row["checkpoint_json"])
            if row["state"] == "completed":
                result = checkpoint.get("sync_result")
            elif row["state"] == "suspended":
                result = "工作暂停，已保留执行记录。"
            else:
                result = checkpoint.get("reason") or row["reason"]
            receipt = {
                "child_id": lease.work_id,
                "state": row["state"],
                "text": str(result or ""),
            }
            saved = await session.scalar(
                select(journal.c.payload_json).where(journal.c.work_id == lease.work_id)
            )
            private_receipt = {
                **receipt,
                "cache_samples": json.loads(saved or "{}")
                .get("metadata", {})
                .get("progress", {})
                .get("cache_samples", []),
            }
            await session.execute(
                update(children)
                .where(children.c.work_id == lease.work_id)
                .values(
                    result_json=encode_json(private_receipt),
                    notified_revision=row["revision"],
                )
            )
            if row["state"] not in {"completed", "failed", "suspended"}:
                return
            parent = (
                (await session.execute(select(work).where(work.c.id == child["root_id"])))
                .mappings()
                .one()
            )
            if parent["state"] in TERMINAL:
                return
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=lease.conversation_id,
                    generation=lease.generation,
                    source_key=f"worker-result:{lease.work_id}:{row['revision']}",
                    work_id=child["root_id"],
                    kind="subagent",
                    ready=True,
                    payload_json=encode_json(
                        {
                            "text": encode_json(
                                {
                                    "child_id": lease.work_id,
                                    "state": row["state"],
                                    "text": receipt["text"],
                                    "detail": "用 subagent_control.result 核对完整产物和执行回执。",
                                }
                            )
                        }
                    ),
                    created=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            if parent["state"] != "running":
                await session.execute(
                    update(work)
                    .where(work.c.id == child["root_id"])
                    .values(state="queued", revision=work.c.revision + 1, updated=time.time())
                )
