"""Original execution references retained across external resource retirement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.db_models import SandboxTaskRunModel


@dataclass(frozen=True)
class TaskResourceSource:
    request_id: str
    conversation_id: str
    source_json: str
    run_id: str | None
    kind: str | None
    resource_request_id: str | None
    peer_id: str | None
    session_id: str | None
    operation_id: str | None = None
    action: str | None = None
    execution_id: str | None = None
    instance_id: str | None = None

    def matches(self, current: TaskResourceSource) -> bool:
        if (
            self.request_id,
            self.conversation_id,
            self.source_json,
            self.kind,
            self.operation_id,
            self.action,
        ) != (
            current.request_id,
            current.conversation_id,
            current.source_json,
            current.kind,
            current.operation_id,
            current.action,
        ) or self.resource_request_id != current.resource_request_id:
            return False
        # A returned identity fills in the same original request, not a new run.
        return all(
            original is None or original == actual
            for original, actual in (
                (self.run_id, current.run_id),
                (self.peer_id, current.peer_id),
                (self.session_id, current.session_id),
                (self.execution_id, current.execution_id),
                (self.instance_id, current.instance_id),
            )
        )


@dataclass(frozen=True)
class ResourceCleanupPreparation:
    conversation_ids: tuple[str, ...]
    work_ids: tuple[str, ...] | None
    tasks: tuple[TaskResourceSource, ...]
    work_sources: tuple[tuple[Any, ...], ...]
    conversation_sources: tuple[tuple[Any, ...], ...]

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(task.request_id for task in self.tasks)

    @property
    def has_external_resources(self) -> bool:
        return any(task.kind in {"external_agent", "acp_prompt"} for task in self.tasks)

    @classmethod
    async def prepare(
        cls,
        session: AsyncSession,
        *,
        conversation_ids: tuple[str, ...] = (),
        work_ids: tuple[str, ...] | None = None,
    ) -> ResourceCleanupPreparation:
        task = SandboxTaskRunModel
        task_scope = (
            task.source_conversation_id.in_(conversation_ids)
            if work_ids is None
            else func.json_extract(task.source_json, "$.work_id").in_(work_ids)
        )
        external = func.json_extract(task.progress_json, "$.kind").in_(
            ("external_agent", "acp_prompt")
        )
        tasks = tuple(
            TaskResourceSource(*row)
            for row in await session.execute(
                select(
                    task.request_id,
                    task.source_conversation_id,
                    task.source_json,
                    task.run_id,
                    *(
                        case(
                            (external, func.json_extract(task.progress_json, f"$.{key}")),
                            else_=None,
                        )
                        for key in (
                            "kind",
                            "resource_request_id",
                            "peer_id",
                            "session_id",
                            "operation_id",
                            "action",
                            "execution_id",
                            "instance_id",
                        )
                    ),
                )
                .where(task_scope)
                .order_by(task.request_id)
            )
        )
        work_scope = (
            work.c.conversation_id.in_(conversation_ids)
            if work_ids is None
            else work.c.id.in_(work_ids)
        )
        work_sources = tuple(
            tuple(row)
            for row in await session.execute(
                select(
                    work.c.id,
                    work.c.conversation_id,
                    work.c.generation,
                    work.c.parent_work_id,
                    work.c.source_key,
                    work.c.source_json,
                )
                .where(work_scope)
                .order_by(work.c.id)
            )
        )
        scopes = conversation_ids or tuple(sorted({str(row[1]) for row in work_sources}))
        conversation = CanonicalConversationModel
        conversation_sources = tuple(
            tuple(row)
            for row in await session.execute(
                select(
                    conversation.id,
                    conversation.person_id,
                    conversation.identity_binding_id,
                    conversation.space_id,
                    conversation.entry_presence_id,
                    conversation.space_binding_id,
                    conversation.thread_id,
                    conversation.generation,
                )
                .where(conversation.id.in_(scopes))
                .order_by(conversation.id)
            )
        )
        return cls(scopes, work_ids, tasks, work_sources, conversation_sources)

    async def validate(self, session: AsyncSession) -> None:
        """Recheck exact current closure/identities using narrow SQL projections."""
        self.verify(
            await self.prepare(
                session, conversation_ids=self.conversation_ids, work_ids=self.work_ids
            )
        )

    def verify(self, current: ResourceCleanupPreparation) -> None:
        if (
            self.work_sources != current.work_sources
            or self.conversation_sources != current.conversation_sources
            or self.request_ids != current.request_ids
            or not all(
                original.matches(actual)
                for original, actual in zip(self.tasks, current.tasks, strict=True)
            )
        ):
            raise ValueError("external_agent_cleanup_source_changed")
