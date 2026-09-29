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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only

from qq_ai_bot.control_plane.media_types import raster_type
from qq_ai_bot.control_plane.paging import Cursor, Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_cursors import decode_integer_cursor_key
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    ControlQueryError,
    DownloadView,
    QueryResourceKind,
)
from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.domain.identity import ConversationId, RequestId
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.persistence.control_execution_query import _key, _page
from qq_ai_bot.persistence.control_paging import numbered_statement
from qq_ai_bot.persistence.unit_of_work import state_revision
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.media_resolver import MediaResolutionError
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


def _integer_key(key: str) -> int:
    value = decode_integer_cursor_key(key, minimum=1)
    if value > 2**63 - 1:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    return value


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

    async def list_plugin_background_turns(
        self, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]:
        if type(plugin_id) is not str or not 1 <= len(plugin_id) <= 128:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        key = _key(request, QueryResourceKind.PLUGIN_BACKGROUND, plugin_id)
        model = PluginBackgroundTurnJobModel
        names = (
            "id",
            "source_event_id",
            "plugin_id",
            "status",
            "attempts",
            "max_attempts",
            "next_attempt_at",
            "lease_until",
            "tool_calls_used",
            "model_requests",
            "last_error_category",
            "created_at",
            "updated_at",
            "completed_at",
            "canonical_target_person_id",
            "canonical_target_space_id",
            "canonical_conversation_id",
            "canonical_presence_id",
        )
        stmt = select(*(getattr(model, name) for name in names)).where(model.plugin_id == plugin_id)
        if key:
            stmt = stmt.where(model.id < _integer_key(key))
        async with self._reader() as session:
            rows = (
                (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt.order_by(model.id.desc()).limit(request.limit + 1),
                                request,
                                order=(model.created_at.desc(), model.id.desc()),
                            )
                        ).statement
                    )
                )
                .mappings()
                .all()
            )
        return _page(
            [
                ActivityView(
                    str(row["id"]),
                    {
                        name: _stamp(value) if isinstance(value, datetime) else value
                        for name, value in row.items()
                    },
                )
                for row in rows[: request.limit]
            ],
            rows,
            request,
            QueryResourceKind.PLUGIN_BACKGROUND,
            plugin_id,
            str(rows[request.limit - 1]["id"]) if len(rows) >= request.limit else None,
            total=sql_window.total,
            number=request.number,
        )

    async def list_participation_feedback(
        self, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]:
        try:
            run_id = RequestId.parse(run_id).text
        except (TypeError, ValueError) as exc:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
        partition = f"{run_id}:{int(include_content)}"
        key = _key(request, QueryResourceKind.PARTICIPATION_FEEDBACK, partition)
        model = InitiativeFeedbackModel
        names = ["sequence", "outcome", "created_at"]
        if include_content:
            names.append("payload_json")
        stmt = select(*(getattr(model, name) for name in names)).where(model.run_id == run_id)
        if key:
            stmt = stmt.where(model.sequence > _integer_key(key))
        async with self._reader() as session:
            if (
                await session.get(
                    InitiativeRunModel, run_id, options=[load_only(InitiativeRunModel.id)]
                )
                is None
            ):
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            rows = (
                (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt.order_by(model.sequence).limit(request.limit + 1),
                                request,
                                order=(model.created_at.desc(), model.sequence.desc()),
                            )
                        ).statement
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows[: request.limit]:
            fields: dict[str, Any] = {
                "run_id": run_id,
                "sequence": row["sequence"],
                "outcome": row["outcome"],
                "created_at": _stamp(row["created_at"]),
            }
            if include_content:
                raw = row["payload_json"]
                try:
                    if len(raw.encode()) > 16384:
                        raise ValueError("oversized feedback")
                    payload = json.loads(raw)
                    if type(payload) is not dict:
                        raise ValueError("invalid feedback")
                    for name in ("actual_targets", "effects"):
                        refs = payload.get(name, [])
                        if (
                            type(refs) is not list
                            or len(refs) > 64
                            or any(type(ref) is not str or not 1 <= len(ref) <= 128 for ref in refs)
                        ):
                            raise ValueError("invalid receipt references")
                        fields[name] = refs
                    sources = payload.get("considered_sources", [])
                    if (
                        type(sources) is not list
                        or len(sources) > 32
                        or any(
                            type(source) is not dict
                            or set(source) != {"kind", "source_id", "revision"}
                            or source["kind"] not in {"event", "memory"}
                            or any(
                                type(source[name]) is not str or len(source[name]) > 128
                                for name in ("source_id", "revision")
                            )
                            for source in sources
                        )
                    ):
                        raise ValueError("invalid source references")
                    fields["considered_sources"] = sources
                except (TypeError, ValueError) as exc:
                    raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
            items.append(ActivityView(f"{run_id}:{row['sequence']}", fields))
        return _page(
            items,
            rows,
            request,
            QueryResourceKind.PARTICIPATION_FEEDBACK,
            partition,
            str(rows[request.limit - 1]["sequence"]) if len(rows) >= request.limit else None,
            total=sql_window.total,
            number=request.number,
        )

    async def list_plugin_outbox(
        self, request: PageRequest, *, plugin_id: str
    ) -> Page[ActivityView]:
        if type(plugin_id) is not str or not 1 <= len(plugin_id) <= 128:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        key = _key(request, QueryResourceKind.PLUGIN_OUTBOX, plugin_id)
        model = PluginNotificationOutboxModel
        # Metadata only: never load message content, media handles or platform IDs.
        stmt = select(
            model.id,
            model.notification_id,
            model.source_event_id,
            model.part_type,
            model.status,
            model.attempts,
            model.max_attempts,
            model.next_attempt_at,
            model.last_error_category,
            model.platform_message_id.is_not(None).label("has_receipt"),
            and_(
                model.canonical_conversation_id.is_not(None),
                model.canonical_presence_id.is_not(None),
                or_(
                    and_(
                        model.canonical_target_person_id.is_not(None),
                        model.canonical_target_space_id.is_(None),
                    ),
                    and_(
                        model.canonical_target_space_id.is_not(None),
                        model.canonical_target_person_id.is_(None),
                    ),
                ),
            ).label("has_owner"),
            model.canonical_conversation_id,
            model.created_at,
            model.updated_at,
        ).where(model.plugin_id == plugin_id)
        if key:
            if (
                not key.isascii()
                or not key.isdecimal()
                or not 1 <= len(key) <= 19
                or not 1 <= int(key) <= 2**63 - 1
            ):
                raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
            stmt = stmt.where(model.id < int(key))
        async with self._reader() as session:
            rows = (
                await session.execute(
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt.order_by(model.id.desc()).limit(request.limit + 1),
                            request,
                            order=(model.created_at.desc(), model.id.desc()),
                        )
                    ).statement
                )
            ).all()
        items = [
            ActivityView(
                str(row.id),
                fields={
                    "outbox_id": row.id,
                    "plugin_id": plugin_id,
                    "notification_id": row.notification_id,
                    "source_event_id": row.source_event_id,
                    "part_type": row.part_type,
                    "status": row.status,
                    "attempts": row.attempts,
                    "max_attempts": row.max_attempts,
                    "next_attempt_at": _stamp(row.next_attempt_at),
                    "last_error_category": row.last_error_category,
                    "has_platform_receipt": row.has_receipt,
                    "conversation_id": row.canonical_conversation_id,
                    "created_at": _stamp(row.created_at),
                    "updated_at": _stamp(row.updated_at),
                    "revision": state_revision(row.updated_at),
                    "can_retry": row.status == "failed"
                    and row.has_owner
                    and not row.has_receipt
                    and row.last_error_category
                    in {"bot_unavailable", "gateway_disconnected", "effect_gate_timeout"}
                    and row.attempts < row.max_attempts,
                },
            )
            for row in rows[: request.limit]
        ]
        return _page(
            items,
            rows,
            request,
            QueryResourceKind.PLUGIN_OUTBOX,
            plugin_id,
            str(rows[request.limit - 1].id) if len(rows) >= request.limit else None,
            total=sql_window.total,
            number=request.number,
        )

    async def list_participation_runs(
        self, request: PageRequest, *, conversation_id: ConversationId | None = None
    ) -> Page[ActivityView]:
        if conversation_id is not None and type(conversation_id) is not ConversationId:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        partition = conversation_id.text if conversation_id else "all"
        key = _key(request, QueryResourceKind.PARTICIPATION, partition)
        stmt = select(InitiativeRunModel).options(
            load_only(
                InitiativeRunModel.id,
                InitiativeRunModel.conversation_id,
                InitiativeRunModel.generation,
                InitiativeRunModel.owner,
                InitiativeRunModel.state,
                InitiativeRunModel.trigger_kind,
                InitiativeRunModel.proposal_id,
                InitiativeRunModel.created_at,
                InitiativeRunModel.updated_at,
            )
        )
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
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt.order_by(
                                InitiativeRunModel.created_at.desc(), InitiativeRunModel.id.desc()
                            ).limit(request.limit + 1),
                            request,
                            order=(
                                InitiativeRunModel.created_at.desc(),
                                InitiativeRunModel.id.desc(),
                            ),
                        )
                    ).statement
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
                total=sql_window.total,
                number=request.number,
            )

    async def download_workspace(self, artifact_id: str) -> DownloadView:
        try:
            metadata, data = await asyncio.to_thread(
                self._store().read_bytes, artifact_id, max_bytes=32 * 1024 * 1024
            )
            return DownloadView(metadata["name"], data, raster_type(data))
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
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt.order_by(work.c.id).limit(request.limit + 1),
                                request,
                                order=(work.c.created.desc(), work.c.id.desc()),
                            )
                        ).statement
                    )
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
                total=sql_window.total,
                number=request.number,
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
                    (
                        sql_window := await numbered_statement(
                            session,
                            stmt.order_by(ModelInvocationModel.id.desc()).limit(request.limit + 1),
                            request,
                            order=(
                                ModelInvocationModel.created_at.desc(),
                                ModelInvocationModel.id.desc(),
                            ),
                        )
                    ).statement
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
                        "cache_creation_input_tokens": row.cache_creation_input_tokens,
                        "cache_creation_5m_input_tokens": row.cache_creation_5m_input_tokens,
                        "cache_creation_1h_input_tokens": row.cache_creation_1h_input_tokens,
                        "total_tokens": row.total_tokens,
                        "physical_request_count": row.physical_request_count,
                        "unknown_usage_request_count": row.unknown_usage_request_count,
                        "native_search_requested": row.native_search_requested,
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
                total=sql_window.total,
                number=request.number,
            )

    async def read_model_usage_summary(self, window: str) -> ActivityView:
        if type(window) is not str:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        duration = {
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }.get(window)
        if duration is None:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        until = datetime.now(UTC)
        since = until - duration
        model = ModelInvocationModel
        measures = (
            func.count(model.id),
            func.coalesce(func.sum(model.prompt_tokens), 0),
            func.coalesce(func.sum(model.completion_tokens), 0),
            func.coalesce(func.sum(model.total_tokens), 0),
            func.coalesce(func.sum(model.cached_prompt_tokens), 0),
            func.count(model.id).filter(model.total_tokens.is_(None)),
            func.coalesce(
                func.sum(model.prompt_tokens).filter(model.cached_prompt_tokens.is_not(None)),
                0,
            ),
            func.count(model.id).filter(
                or_(model.cached_prompt_tokens.is_(None), model.prompt_tokens.is_(None))
            ),
            func.coalesce(
                func.sum(model.cached_prompt_tokens).filter(model.prompt_tokens.is_not(None)),
                0,
            ),
            func.coalesce(func.sum(model.physical_request_count), 0),
            func.count(model.id).filter(model.physical_request_count.is_(None)),
            func.coalesce(func.sum(model.unknown_usage_request_count), 0),
            func.count(model.id).filter(model.native_search_requested.is_(True)),
            func.count(model.id).filter(model.native_search_requested.is_(None)),
            func.coalesce(
                func.sum(model.cache_creation_input_tokens).filter(
                    func.lower(model.provider) == "anthropic"
                ),
                0,
            ),
            func.count(model.id).filter(
                func.lower(model.provider) == "anthropic",
                or_(
                    model.prompt_tokens.is_(None),
                    model.cached_prompt_tokens.is_(None),
                    model.cache_creation_input_tokens.is_(None),
                ),
            ),
            func.coalesce(
                func.sum(model.cache_creation_input_tokens).filter(
                    func.lower(model.provider) == "anthropic",
                    model.prompt_tokens.is_not(None),
                    model.cached_prompt_tokens.is_not(None),
                ),
                0,
            ),
            func.coalesce(
                func.sum(model.cache_creation_5m_input_tokens).filter(
                    func.lower(model.provider) == "anthropic"
                ),
                0,
            ),
            func.coalesce(
                func.sum(model.cache_creation_1h_input_tokens).filter(
                    func.lower(model.provider) == "anthropic"
                ),
                0,
            ),
            func.count(model.id).filter(
                func.lower(model.provider) == "anthropic",
                model.cache_creation_input_tokens > 0,
                or_(
                    model.cache_creation_5m_input_tokens.is_(None),
                    model.cache_creation_1h_input_tokens.is_(None),
                    model.cache_creation_input_tokens
                    != model.cache_creation_5m_input_tokens + model.cache_creation_1h_input_tokens,
                ),
            ),
            func.count(model.id).filter(
                func.lower(model.provider) == "anthropic",
                model.cache_creation_5m_input_tokens.is_not(None),
            ),
            func.count(model.id).filter(
                func.lower(model.provider) == "anthropic",
                model.cache_creation_1h_input_tokens.is_not(None),
            ),
        )
        period = (model.created_at >= since, model.created_at < until)
        bucket = func.strftime(
            "%Y-%m-%dT%H:00:00Z" if window == "24h" else "%Y-%m-%d",
            model.created_at,
        )
        async with self._reader() as session:
            totals = (await session.execute(select(*measures).where(*period))).one()
            groups = (
                await session.execute(
                    select(model.provider, model.model, *measures)
                    .where(*period)
                    .group_by(model.provider, model.model)
                    .order_by(func.sum(model.total_tokens).desc())
                )
            ).all()
            profiles = (
                await session.execute(
                    select(model.profile_id, model.provider, model.model, *measures)
                    .where(*period)
                    .group_by(model.profile_id, model.provider, model.model)
                    .order_by(func.sum(model.total_tokens).desc())
                )
            ).all()
            tasks = (
                await session.execute(
                    select(model.task, *measures)
                    .where(*period)
                    .group_by(model.task)
                    .order_by(func.sum(model.total_tokens).desc())
                )
            ).all()
            buckets = (
                await session.execute(
                    select(bucket, *measures).where(*period).group_by(bucket).order_by(bucket)
                )
            ).all()
            model_buckets = (
                await session.execute(
                    select(model.provider, model.model, bucket, *measures)
                    .where(*period)
                    .group_by(model.provider, model.model, bucket)
                    .order_by(model.provider, model.model, bucket)
                )
            ).all()

        def usage(values: Any) -> dict[str, int | float | str | None]:
            reported_input = int(values[6] or 0)
            reported_cached = int(values[8] or 0)
            native_search_invocations = int(values[12] or 0)
            return {
                "calls": int(values[0] or 0),
                "physical_requests": int(values[9] or 0),
                "physical_requests_unreported_calls": int(values[10] or 0),
                "unknown_usage_requests": int(values[11] or 0),
                "native_search_invocations": native_search_invocations,
                "native_search_unreported_calls": int(values[13] or 0),
                "native_search_cost": None,
                "native_search_cost_status": (
                    "not_reported"
                    if native_search_invocations
                    else "unknown_historical"
                    if values[13]
                    else "not_applicable"
                ),
                "input_tokens": int(values[1] or 0),
                "output_tokens": int(values[2] or 0),
                "total_tokens": int(values[3] or 0),
                "cached_input_tokens": int(values[4] or 0),
                "missing_usage_calls": int(values[5] or 0),
                "cache_reported_input_tokens": reported_input,
                "cache_unreported_calls": int(values[7] or 0),
                "cache_reported_cached_tokens": reported_cached,
                "cache_reported_uncached_tokens": max(0, reported_input - reported_cached),
                "cache_hit_rate": reported_cached / reported_input if reported_input else None,
                "cache_write_input_tokens": int(values[14] or 0),
                "cache_write_unreported_calls": int(values[15] or 0),
                # This subset can be split from the chart's reported non-read input.
                "cache_write_classified_input_tokens": int(values[16] or 0),
                "cache_write_5m_input_tokens": int(values[17] or 0),
                "cache_write_1h_input_tokens": int(values[18] or 0),
                "cache_write_ttl_unreported_calls": int(values[19] or 0),
                "cache_write_5m_reported_calls": int(values[20] or 0),
                "cache_write_1h_reported_calls": int(values[21] or 0),
            }

        return ActivityView(
            window,
            {
                "window": window,
                "since": since.isoformat(),
                "until": until.isoformat(),
                **usage(totals),
                "models": [
                    {"provider": row[0], "model": row[1], **usage(row[2:])} for row in groups
                ],
                "profiles": [
                    {
                        "profile_id": row[0],
                        "provider": row[1],
                        "model": row[2],
                        **usage(row[3:]),
                    }
                    for row in profiles
                ],
                "tasks": [{"task": row[0], **usage(row[1:])} for row in tasks],
                "buckets": [{"at": row[0], **usage(row[1:])} for row in buckets],
                "model_buckets": [
                    {"provider": row[0], "model": row[1], "at": row[2], **usage(row[3:])}
                    for row in model_buckets
                ],
            },
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
                number=request.number,
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
                total=result.get("total"),
                number=request.number,
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
