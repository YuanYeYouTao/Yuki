"""Bounded reviewed activity projections over existing sources of truth."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import stat
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from qq_ai_bot.control_plane.paging import Cursor, Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    ControlQueryError,
    DownloadView,
    QueryResourceKind,
)
from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.domain.identity import ConversationId
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.persistence.control_execution_query import _key, _page
from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel, AutomationStepRunModel
from qq_ai_bot.persistence.unit_of_work import state_revision
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.media_resolver import MediaResolutionError
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


def _read_media(path: Path, digest: str | None) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32 * 1024 * 1024:
            raise ValueError("unsafe media")
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or (digest and hashlib.sha256(data).hexdigest() != digest):
        raise ValueError("media mismatch")
    return data


class ControlActivityQueryAdapter:
    def __init__(
        self,
        reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        workspace: WorkspaceStore | None,
        conversation_media: ConversationMediaService | None = None,
    ) -> None:
        self._reader = reader
        self._workspace = workspace
        self._media = conversation_media

    async def list_participation_runs(
        self, request: PageRequest, *, conversation_id: ConversationId | None = None
    ) -> Page[ActivityView]:
        if conversation_id is not None and type(conversation_id) is not ConversationId:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        partition = conversation_id.text if conversation_id else "all"
        key = _key(request, QueryResourceKind.PARTICIPATION, partition)
        stmt = select(InitiativeRunModel)
        if conversation_id:
            stmt = stmt.where(InitiativeRunModel.conversation_id == conversation_id.text)
        if key:
            try:
                at, identity = json.loads(key)
                cursor_at = datetime.fromisoformat(at)
                if (
                    cursor_at.tzinfo is None
                    or type(identity) is not str
                    or not 1 <= len(identity) <= 128
                ):
                    raise ValueError("invalid participation cursor")
                stmt = stmt.where(
                    or_(
                        InitiativeRunModel.created_at < cursor_at,
                        and_(
                            InitiativeRunModel.created_at == cursor_at,
                            InitiativeRunModel.id < identity,
                        ),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
        async with self._reader() as session:
            rows = list(
                await session.scalars(
                    stmt.order_by(
                        InitiativeRunModel.created_at.desc(), InitiativeRunModel.id.desc()
                    ).limit(request.limit + 1)
                )
            )
            selected = rows[: request.limit]
            feedback_alias = aliased(InitiativeFeedbackModel)
            outcomes = (
                list(
                    await session.execute(
                        select(InitiativeFeedbackModel.run_id, InitiativeFeedbackModel.outcome)
                        .where(InitiativeFeedbackModel.run_id.in_([row.id for row in selected]))
                        .where(
                            InitiativeFeedbackModel.sequence
                            == (
                                select(func.max(feedback_alias.sequence))
                                .where(feedback_alias.run_id == InitiativeFeedbackModel.run_id)
                                .correlate(InitiativeFeedbackModel)
                                .scalar_subquery()
                            )
                        )
                    )
                )
                if selected
                else []
            )
            feedback = {row.run_id: row.outcome for row in outcomes}
            items = [
                ActivityView(
                    row.id,
                    {
                        "conversation_id": row.conversation_id,
                        "generation": row.generation,
                        "owner": row.owner,
                        "state": row.state,
                        "trigger_kind": row.trigger_kind,
                        "proposal_id": row.proposal_id,
                        "feedback": feedback.get(row.id),
                        "created_at": _stamp(row.created_at),
                        "updated_at": _stamp(row.updated_at),
                    },
                )
                for row in selected
            ]
            last = selected[-1] if selected else None
            return _page(
                items,
                rows,
                request,
                QueryResourceKind.PARTICIPATION,
                partition,
                json.dumps([_stamp(last.created_at), last.id], separators=(",", ":"))
                if last
                else None,
            )

    async def download_workspace(self, artifact_id: str) -> DownloadView:
        try:
            metadata, data = await asyncio.to_thread(
                self._store().read_bytes, artifact_id, max_bytes=32 * 1024 * 1024
            )
            return DownloadView(metadata["name"], data)
        except WorkspaceError as exc:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND)) from exc

    async def download_chat_media(
        self, conversation_id: ConversationId, event_id: int, attachment_index: int
    ) -> DownloadView:
        if (
            type(conversation_id) is not ConversationId
            or type(event_id) is not int
            or not 1 <= event_id <= 2**63 - 1
            or type(attachment_index) is not int
            or not 0 <= attachment_index <= 1024
        ):
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        if self._media is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        try:
            item, path = await self._media.authorized_path(
                event_id=event_id,
                attachment_index=attachment_index,
                conversation_id=conversation_id.text,
                generation=None,
                gateway=None,
            )
            data = await asyncio.to_thread(_read_media, path, item.content_sha256)
            # Revalidate generation and expiry after reading before handing bytes to HTTP.
            await self._media.authorized_path(
                event_id=event_id,
                attachment_index=attachment_index,
                conversation_id=conversation_id.text,
                generation=item.generation,
                gateway=None,
            )
            media_type = "application/octet-stream"
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                media_type = "image/png"
            elif data.startswith(b"\xff\xd8\xff"):
                media_type = "image/jpeg"
            elif data.startswith((b"GIF87a", b"GIF89a")):
                media_type = "image/gif"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                media_type = "image/webp"
            return DownloadView(
                (item.display_name or f"attachment-{attachment_index}")[:128], data, media_type
            )
        except (ConversationMediaError, MediaResolutionError, OSError, ValueError) as exc:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND)) from exc

    async def list_work(
        self, request: PageRequest, *, include_content: bool = False
    ) -> Page[ActivityView]:
        key = _key(request, QueryResourceKind.WORK, "work")
        columns = [
            work.c[name]
            for name in (
                "id",
                "conversation_id",
                "generation",
                "revision",
                "state",
                "reason",
                "model_requests",
                "tool_calls",
                "active_seconds",
                "sent_messages",
                "created",
                "updated",
            )
        ]
        if include_content:
            columns.append(work.c.goal)
        stmt = select(*columns)
        if key:
            stmt = stmt.where(work.c.id > key)
        async with self._reader() as session:
            rows = list(
                (
                    await session.execute(stmt.order_by(work.c.id).limit(request.limit + 1))
                ).mappings()
            )
            selected = rows[: request.limit]
            items = []
            for row in selected:
                fields: dict[str, Any] = {
                    str(col.name): row[col.name] for col in columns if col.name != "id"
                }
                for name in ("created", "updated"):
                    fields[name] = datetime.fromtimestamp(fields[name], UTC).isoformat()
                items.append(ActivityView(row["id"], fields))
            return _page(
                items,
                rows,
                request,
                QueryResourceKind.WORK,
                "work",
                selected[-1]["id"] if selected else None,
            )

    async def read_automation(self, automation_id: int) -> ActivityView:
        if type(automation_id) is not int or not 1 <= automation_id <= 2**63 - 1:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        async with self._reader() as session:
            row = await session.get(AutomationModel, automation_id)
            if row is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            runs = list(
                await session.scalars(
                    select(AutomationRunModel)
                    .where(AutomationRunModel.automation_id == automation_id)
                    .order_by(AutomationRunModel.id.desc())
                    .limit(20)
                )
            )
            steps = (
                list(
                    await session.scalars(
                        select(AutomationStepRunModel)
                        .where(AutomationStepRunModel.run_id.in_([item.id for item in runs]))
                        .order_by(AutomationStepRunModel.id.desc())
                        .limit(200)
                    )
                )
                if runs
                else []
            )
            # Do not expose authority snapshots, transport IDs or raw result payloads.
            return ActivityView(
                str(row.id),
                {
                    "name": row.name,
                    "revision": state_revision(row.updated_at),
                    "status": row.status,
                    "creator_kind": row.creator_kind,
                    "creator_person_id": row.canonical_creator_person_id,
                    "target_person_id": row.canonical_target_person_id,
                    "target_space_id": row.canonical_target_space_id,
                    "timezone": row.timezone,
                    "schedule": json.loads(row.schedule_json),
                    "script": json.loads(row.script_json),
                    "script_hash": row.script_hash,
                    "next_run_at": _stamp(row.next_run_at),
                    "last_run_at": _stamp(row.last_run_at),
                    "run_count": row.run_count,
                    "consecutive_failures": row.consecutive_failures,
                    "runs": [
                        {
                            "id": item.id,
                            "status": item.status,
                            "scheduled_for": _stamp(item.scheduled_for),
                            "started_at": _stamp(item.actual_started_at),
                            "finished_at": _stamp(item.finished_at),
                            "model_calls": item.llm_calls,
                            "tool_calls": item.tool_calls,
                            "sent_messages": item.messages_sent,
                            "error_category": item.error_category,
                        }
                        for item in runs
                    ],
                    "steps": [
                        {
                            "id": item.id,
                            "run_id": item.run_id,
                            "step_id": item.step_id,
                            "capability": item.capability,
                            "status": item.status,
                            "started_at": _stamp(item.started_at),
                            "finished_at": _stamp(item.finished_at),
                            "error_category": item.error_category,
                        }
                        for item in steps
                    ],
                    "runs_limit": 20,
                    "steps_limit": 200,
                },
            )

    async def list_model_usage(self, request: PageRequest) -> Page[ActivityView]:
        key = _key(request, QueryResourceKind.MODEL_USAGE, "usage")
        if key and (not key.isascii() or not key.isdigit() or not 1 <= int(key) <= 2**63 - 1):
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        stmt = select(ModelInvocationModel)
        if key:
            stmt = stmt.where(ModelInvocationModel.id < int(key))
        async with self._reader() as session:
            rows = list(
                await session.scalars(
                    stmt.order_by(ModelInvocationModel.id.desc()).limit(request.limit + 1)
                )
            )
            selected = rows[: request.limit]
            items = [
                ActivityView(
                    str(row.id),
                    {
                        "task": row.task,
                        "profile_id": row.profile_id,
                        "provider": row.provider,
                        "model": row.model,
                        "success": row.success,
                        "prompt_tokens": row.prompt_tokens,
                        "completion_tokens": row.completion_tokens,
                        "cached_prompt_tokens": row.cached_prompt_tokens,
                        "total_tokens": row.total_tokens,
                        "latency_seconds": row.latency_seconds,
                        "error_category": row.error_category,
                        "created_at": _stamp(row.created_at),
                        "turn_id": row.runtime_turn_id,
                        "conversation_id": row.canonical_conversation_id,
                    },
                )
                for row in selected
            ]
            return _page(
                items,
                rows,
                request,
                QueryResourceKind.MODEL_USAGE,
                "usage",
                str(selected[-1].id) if selected else None,
            )

    def _store(self) -> WorkspaceStore:
        if self._workspace is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        return self._workspace

    async def list_workspace(self, request: PageRequest) -> Page[ActivityView]:
        try:
            result = await asyncio.to_thread(
                self._store().list,
                cursor=request.cursor.value if request.cursor else "",
                limit=request.limit,
            )
            return Page(
                [
                    ActivityView(
                        item["artifact_id"],
                        {
                            key: item[key]
                            for key in (
                                "name",
                                "size",
                                "sha256",
                                "revision",
                                "created_at",
                                "modified_at",
                                "expires_at",
                                "immutable",
                            )
                        },
                    )
                    for item in result["items"]
                ],
                next_cursor=Cursor(result["next_cursor"]) if result["next_cursor"] else None,
                snapshot_at=datetime.now(UTC),
            )
        except WorkspaceError as exc:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc

    async def read_workspace(self, artifact_id: str) -> ActivityView:
        try:
            metadata, data = await asyncio.to_thread(
                self._store().read_bytes, artifact_id, max_bytes=1024 * 1024
            )
            fields = {
                key: metadata[key] for key in ("name", "size", "sha256", "revision", "immutable")
            }
            try:
                content = codecs.getincrementaldecoder("utf-8")().decode(
                    data[:32768], final=len(data) <= 32768
                )
                if "\x00" in content:
                    raise UnicodeError("binary")
                fields.update(text=content, binary=False, truncated=len(data) > 32768)
            except UnicodeError:
                fields.update(binary=True)
            return ActivityView(artifact_id, fields)
        except WorkspaceError as exc:
            code = (
                ProblemCode.NOT_FOUND
                if str(exc) in {"artifact_not_found", "artifact_expired"}
                else ProblemCode.VALIDATION_ERROR
            )
            raise ControlQueryError(Problem(code)) from exc
