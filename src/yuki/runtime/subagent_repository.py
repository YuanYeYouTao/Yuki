"""Worker admission, independent leases and durable parent/child mail."""

from __future__ import annotations

import builtins
import json
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.runtime.subagent_schema import children, media, media_refs
from yuki.runtime.work_repository import (
    TERMINAL,
    WorkConflict,
    WorkLease,
    WorkRepository,
    encode_json,
)
from yuki.runtime.work_schema_v1 import effects, inputs, journal, work
from yuki.runtime.work_tree import ancestors, descendants, rooted_tree
from yuki.runtime.work_wait_schema import waits
from yuki.sandbox.resource_cleanup import ResourceCleanupPreparation


class SubagentRepository:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository
        self.database = repository.database

    async def start(self, lease: WorkLease, parent_id: str, key: str, brief: dict[str, Any]) -> str:
        if lease.work_id is not None and lease.work_id != parent_id:
            raise ValueError("subagent_not_owned")
        goal = brief.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("invalid_subagent_goal")
        encoded = encode_json(brief)
        async with self.database.sessions() as reader:
            snapshot = (
                (await reader.execute(select(work).where(work.c.id == parent_id))).mappings().one()
            )
        identity = str(uuid4())
        source = json.loads(snapshot["source_json"])
        source.update(work_id=identity, parent_work_id=parent_id, worker=True)
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            root = (
                (await session.execute(select(work).where(work.c.id == parent_id))).mappings().one()
            )
            if (
                root["conversation_id"] != lease.conversation_id
                or root["generation"] != lease.generation
                or root["state"] in TERMINAL
                or root["source_json"] != snapshot["source_json"]
            ):
                raise WorkConflict("subagent_parent_unavailable")
            previous = (
                (await session.execute(select(children).where(children.c.source_key == key)))
                .mappings()
                .first()
            )
            if previous:
                actual_parent = await session.scalar(
                    select(work.c.parent_work_id).where(work.c.id == previous["work_id"])
                )
                if actual_parent != parent_id or previous["brief_json"] != encoded:
                    raise WorkConflict("subagent_start_conflict")
                return str(previous["work_id"])
            if not self.database.subagents_enabled:
                raise ValueError("subagent_admission_disabled")
            await self.repository.accept_in_session(
                session,
                lease,
                identity=identity,
                parent_work_id=parent_id,
                source_key=f"worker:{identity}",
                source=source,
                goal=goal,
                initial_state="queued",
            )
            await session.execute(
                insert(children).values(work_id=identity, source_key=key, brief_json=encoded)
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
            states = (
                {"completed", "failed", "cancelled", "suspended", "waiting_user"}
                if reconcile
                else {"queued", "running"}
            )
            if row is None or row["state"] not in states:
                return None
            actual = await session.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.id == row["conversation_id"]
                )
            )
            if actual != row["generation"]:
                if reconcile:
                    return None
                await session.execute(
                    update(work)
                    .where(work.c.id == identity)
                    .values(
                        state="cancelled",
                        reason="generation_obsolete",
                        checkpoint_json=func.json_remove(
                            work.c.checkpoint_json, "$.accepted_control"
                        ),
                        updated=now,
                    )
                )
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
                    select(children.c.work_id).where(
                        children.c.archived_at.is_(None),
                        select(work.c.id)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id == work.c.conversation_id,
                        )
                        .where(
                            work.c.id == children.c.work_id,
                            work.c.generation == CanonicalConversationModel.generation,
                            work.c.state.in_(
                                ("completed", "failed", "cancelled", "suspended", "waiting_user")
                            ),
                            children.c.notified_revision < work.c.revision,
                        )
                        .exists(),
                    )
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

        def expired_query(root_ids: list[str] | None = None) -> Select[Any]:
            root = work.alias("archive_root")
            lineage = ancestors(children.c.work_id, include_self=True).order_by(None).subquery()
            root_query = select(
                select(lineage.c.id)
                .where(
                    select(root.c.id)
                    .where(root.c.id == lineage.c.id, root.c.parent_work_id.is_(None))
                    .exists()
                )
                .correlate(children)
                .scalar_subquery()
                .label("id")
            ).where(children.c.archived_at.is_(None), children.c.lease_until <= now)
            roots = (
                root_query.distinct().subquery()
                if root_ids is None
                else select(work.c.id).where(work.c.id.in_(root_ids)).subquery()
            )
            tree = rooted_tree(
                select(roots.c.id).where(
                    select(root.c.id)
                    .where(
                        root.c.id == roots.c.id,
                        root.c.parent_work_id.is_(None),
                        root.c.state.in_(TERMINAL),
                        root.c.updated < cutoff,
                    )
                    .exists()
                )
            )
            retained = tree.alias("retained_tree")
            return (
                select(tree.c.work_id, tree.c.root_id)
                .where(
                    select(children.c.work_id)
                    .where(
                        children.c.work_id == tree.c.work_id,
                        children.c.archived_at.is_(None),
                        children.c.lease_until <= now,
                    )
                    .exists(),
                    select(work.c.id)
                    .where(work.c.id == tree.c.work_id, work.c.state.in_(TERMINAL))
                    .exists(),
                    tree.c.root_id.not_in(
                        select(retained.c.root_id).where(
                            select(work.c.id)
                            .where(work.c.id == retained.c.work_id, work.c.state.not_in(TERMINAL))
                            .exists()
                        )
                    ),
                )
                .limit(16)
            )

        async def roots_for_archive(
            session: AsyncSession, expired: list[str], root_ids: list[str]
        ) -> list[str]:
            tree = rooted_tree(select(work.c.id).where(work.c.id.in_(root_ids)))
            return list(
                await session.scalars(
                    select(tree.c.root_id)
                    .where(
                        tree.c.work_id.in_(expired),
                        tree.c.root_id.not_in(
                            select(tree.c.root_id)
                            .join(children, children.c.work_id == tree.c.work_id)
                            .where(
                                children.c.archived_at.is_(None),
                                children.c.work_id.not_in(expired),
                            )
                        ),
                        select(work.c.id)
                        .where(
                            work.c.id == tree.c.root_id,
                            work.c.state.in_(TERMINAL),
                            work.c.updated < cutoff,
                        )
                        .exists(),
                    )
                    .distinct()
                )
            )

        # Most passes have nothing to archive. Scan before reserving SQLite's
        # single writer, then check eligibility again after acquiring it.
        async with self.database.sessions() as session:
            candidates = (await session.execute(expired_query())).all()
            if not candidates:
                return
            root_ids = list({root_id for _, root_id in candidates})
            original_expired = [identity for identity, _ in candidates]
            original_roots = await roots_for_archive(session, original_expired, root_ids)
            original_ids = tuple(sorted({*original_expired, *original_roots}))
            resources = await ResourceCleanupPreparation.prepare(session, work_ids=original_ids)
        if resources.has_peers:
            client = self.database.sandbox_client
            if client is None:
                raise ValueError("external_agent_cleanup_unconfirmed")
            await client.close_peers_for_requests(
                resources.request_ids, f"work-archive:{','.join(original_ids)}", purge=True
            )
        async with self.database.immediate_session() as session:
            prepared_query = expired_query(root_ids)
            expired = list(
                await session.scalars(
                    prepared_query.where(
                        prepared_query.selected_columns.work_id.in_(
                            [identity for identity, _ in candidates]
                        )
                    )
                )
            )
            if not expired:
                return
            expired_roots = await roots_for_archive(session, expired, root_ids)
            archived_work_ids = [*expired, *expired_roots]
            current = await ResourceCleanupPreparation.prepare(
                session, work_ids=tuple(archived_work_ids)
            )
            if resources.has_peers:
                if tuple(sorted(set(archived_work_ids))) != original_ids:
                    raise ValueError("external_agent_cleanup_source_changed")
                resources.verify(current)
            elif current.has_peers:
                raise ValueError("external_agent_cleanup_source_changed")
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
            from yuki.runtime.protocol_schema import refs as protocol_refs

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
        self, parent_id: str, identity: str, *, include_checkpoint: bool = False
    ) -> dict[str, Any]:
        async with self.database.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(
                            children,
                            work.c.state,
                            work.c.parent_work_id,
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
                        .where(
                            work.c.id.in_(descendants(parent_id)), children.c.work_id == identity
                        )
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
        self,
        parent_id: str | None,
        *,
        lease: WorkLease | None = None,
        source: dict[str, Any] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Internal consumers read all owned children; model directory is explicitly paged."""
        if parent_id is None:
            from yuki.runtime.work_queries import WorkQueries

            assert lease is not None and source is not None
            scope = WorkQueries._query(lease, source, local=True, include_workers=True)
            owned = scope.with_only_columns(work.c.id)
        else:
            owned = descendants(parent_id)
        result: list[dict[str, Any]] = []
        async with self.database.sessions() as session:
            while True:
                size = limit if limit is not None else 50
                query = (
                    select(
                        children,
                        work.c.state,
                        work.c.parent_work_id,
                        work.c.goal,
                        work.c.model_requests,
                        work.c.tool_calls,
                    )
                    .join(work, children.c.work_id == work.c.id)
                    .where(work.c.id.in_(owned))
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

    async def unfinished(self, parent_id: str) -> builtins.list[dict[str, Any]]:
        """Read actual unfinished descendants without directory pagination."""
        async with self.database.sessions() as session:
            return [
                dict(row)
                for row in (
                    await session.execute(
                        select(children, work.c.state)
                        .join(work, work.c.id == children.c.work_id)
                        .where(work.c.id.in_(descendants(parent_id)), work.c.state.not_in(TERMINAL))
                    )
                ).mappings()
            ]

    async def message(
        self,
        lease: WorkLease,
        parent_id: str,
        identity: str,
        key: str,
        text: str | None,
        *,
        ask: bool = False,
        reply_to: str | None = None,
        to_parent: bool = False,
    ) -> dict[str, Any]:
        if text is not None and not text.strip():
            raise ValueError("invalid_subagent_message")
        sender = identity if to_parent else parent_id
        if lease.work_id and lease.work_id != sender:
            raise ValueError("subagent_not_owned")
        await self.related(parent_id, identity)
        destination = parent_id if to_parent else identity
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
            if (
                target["conversation_id"] != lease.conversation_id
                or target["generation"] != lease.generation
            ):
                raise ValueError("subagent_not_owned")
            if not to_parent and target["state"] == "cancelled":
                raise ValueError("subagent_message_target_terminal")
            waiting_parent = ask and to_parent and target["state"] not in TERMINAL
            if text is None:
                return {"message_id": key, "waiting_parent": waiting_parent}
            payload = {
                "text": encode_json(
                    {
                        "kind": "subagent_question" if ask else "subagent_message",
                        "child_id": identity,
                        "message_id": key,
                        "sender_work_id": sender,
                        "target_work_id": destination,
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
            waiting = await session.scalar(
                select(waits.c.id)
                .where(waits.c.work_id == destination, waits.c.status == "active")
                .limit(1)
            )
            business_input = ask or not to_parent
            if inserted is not None and (
                target["state"] in {"waiting_external", "waiting_user"}
                if to_parent and ask
                else target["state"] == "waiting_external" and waiting is None
                if to_parent
                else target["state"] != "running"
            ):
                if business_input:
                    await session.execute(
                        update(waits)
                        .where(waits.c.work_id == destination, waits.c.status == "active")
                        .values(status="cancelled", updated=time.time())
                    )
                await session.execute(
                    update(work)
                    .where(work.c.id == destination)
                    .values(
                        state="queued",
                        revision=work.c.revision + 1,
                        checkpoint_json=func.json_remove(
                            work.c.checkpoint_json, "$.accepted_control"
                        )
                        if business_input
                        else work.c.checkpoint_json,
                        updated=time.time(),
                    )
                )
            return {
                "message_id": key,
                "waiting_parent": waiting_parent,
            }

    async def resume(
        self,
        lease: WorkLease,
        parent_id: str,
        identity: str,
        *,
        key: str,
        instruction: str | None = None,
    ) -> None:
        """Queue this retained child without changing any ancestor's lifecycle."""
        if instruction:
            await self.message(lease, parent_id, identity, key, instruction)
            return
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            row = (
                (
                    await session.execute(
                        select(work, children.c.archived_at)
                        .join(children, children.c.work_id == work.c.id)
                        .where(work.c.id == identity, work.c.id.in_(descendants(parent_id)))
                    )
                )
                .mappings()
                .one()
            )
            if row["archived_at"] is not None:
                raise ValueError("subagent_archived")
            if row["state"] == "cancelled":
                raise ValueError("subagent_not_resumable")
            if (
                row["conversation_id"] != lease.conversation_id
                or row["generation"] != lease.generation
            ):
                raise ValueError("subagent_not_owned")
            await session.execute(
                update(waits)
                .where(waits.c.work_id == identity, waits.c.status == "active")
                .values(status="cancelled", updated=time.time())
            )
            if row["state"] != "running":
                await session.execute(
                    update(work)
                    .where(work.c.id == identity)
                    .values(
                        state="queued",
                        reason="explicit_resume",
                        checkpoint_json=func.json_remove(
                            work.c.checkpoint_json, "$.accepted_control"
                        ),
                        revision=work.c.revision + 1,
                        updated=time.time(),
                    )
                )

    async def cancel(self, lease: WorkLease, parent_id: str, identity: str) -> None:
        from yuki.runtime.work_management import manage_work

        await self.related(parent_id, identity)
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            row = (
                (await session.execute(select(work).where(work.c.id == identity))).mappings().one()
            )
            await manage_work(session, identity, row["revision"], "cancel")

    async def finish(self, lease: WorkLease) -> None:
        """Notify the parent with the committed result of this exact revision."""
        async with self.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            row = (
                (await session.execute(select(work).where(work.c.id == lease.work_id)))
                .mappings()
                .one()
            )
            checkpoint = json.loads(row["checkpoint_json"])
            if row["state"] == "completed":
                result = checkpoint.get("sync_result")
            elif row["state"] == "waiting_user":
                result = checkpoint.get("reason")
            else:
                result = checkpoint.get("reason") or row["reason"]
            receipt = {
                "child_id": lease.work_id,
                "state": row["state"],
                "text": str(result or ""),
            }
            await session.execute(
                update(children)
                .where(children.c.work_id == lease.work_id)
                .values(
                    result_json=encode_json(receipt),
                    notified_revision=row["revision"],
                )
            )
            if row["state"] not in {
                "completed",
                "failed",
                "cancelled",
                "suspended",
                "waiting_user",
            }:
                return
            parent = (
                (await session.execute(select(work).where(work.c.id == row["parent_work_id"])))
                .mappings()
                .one()
            )
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=lease.conversation_id,
                    generation=lease.generation,
                    source_key=f"worker-result:{lease.work_id}:{row['revision']}",
                    work_id=row["parent_work_id"],
                    kind="subagent",
                    ready=True,
                    payload_json=encode_json(
                        {
                            "text": encode_json(
                                {
                                    "child_id": lease.work_id,
                                    "state": row["state"],
                                    "text": receipt["text"],
                                    "detail": (
                                        "按需用 subagent_control(action=result, child_id=原ID) "
                                        "查询原结果与执行回执。"
                                    ),
                                }
                            )
                        }
                    ),
                    created=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            waiting = await session.scalar(
                select(waits.c.id)
                .where(waits.c.work_id == row["parent_work_id"], waits.c.status == "active")
                .limit(1)
            )
            if parent["state"] == "waiting_external" and waiting is None:
                await session.execute(
                    update(work)
                    .where(work.c.id == row["parent_work_id"])
                    .values(state="queued", revision=work.c.revision + 1, updated=time.time())
                )
