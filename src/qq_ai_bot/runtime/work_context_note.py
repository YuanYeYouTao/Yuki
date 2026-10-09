"""Optional sourced observations; never task authority or execution receipts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import select, text

from qq_ai_bot.runtime.work_compaction import SourcedFact

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


class ContextNote(BaseModel):
    version: int = 1
    facts: list[SourcedFact] = Field(default_factory=list)
    unresolved: list[SourcedFact] = Field(default_factory=list)
    next_steps: list[SourcedFact] = Field(default_factory=list)


async def validate_note(
    control: WorkControl, value: Any
) -> tuple[dict[str, Any], tuple[str, ...], int, int]:
    note = ContextNote.model_validate(value).model_dump()
    refs: set[str] = set()
    for section in ("facts", "unresolved", "next_steps"):
        for fact in note[section]:
            refs.update(fact["refs"])
    if control.current is None:
        raise ValueError("no_active_work")
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
    from qq_ai_bot.persistence.models import ChatEventModel, ToolArtifactModel
    from qq_ai_bot.runtime.subagent_schema import children
    from qq_ai_bot.runtime.work_schema_v1 import effects, inputs
    from qq_ai_bot.tool_results.access import access_from_source
    from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository

    identity = control.current["id"]
    source = json.loads(control.current["source_json"])
    if control.context_access is not None and (
        source.get("actor_person_id", "") != control.context_access.actor_person_id
        or source.get("principal_kind", "person") != control.context_access.principal_kind
    ):
        raise ValueError("work_context_note_actor_mismatch")
    selected_events = {
        source.get("trigger_event_id"),
        *(control.session.event_ids if control.session is not None else ()),
        *(getattr(control.session, "public_event_ids", ()) if control.session is not None else ()),
    }
    handles = []
    async with control.repository.database.sessions() as reader:
        await reader.execute(text("BEGIN"))
        conversation = await reader.get(CanonicalConversationModel, control.lease.conversation_id)
        if conversation is None or conversation.generation != control.lease.generation:
            raise ValueError("work_context_note_source_changed")
        source_revision = conversation.prompt_source_revision
        privacy = int(
            await reader.scalar(
                select(ExecutionTraceStateModel.privacy_generation).where(
                    ExecutionTraceStateModel.id == 1
                )
            )
            or 0
        )
        for ref in sorted(refs):
            if ref == "goal":
                continue
            kind, _, key = ref.partition(":")
            valid: Any = None
            if kind == "input" and key.isdecimal():
                valid = await reader.scalar(
                    select(inputs.c.id).where(
                        inputs.c.id == int(key),
                        inputs.c.work_id == identity,
                        inputs.c.generation == control.lease.generation,
                        inputs.c.state.in_(("staged", "consumed")),
                    )
                )
            elif kind == "event" and key.isdecimal() and int(key) in selected_events:
                valid = await reader.scalar(
                    select(ChatEventModel.id).where(
                        ChatEventModel.id == int(key),
                        ChatEventModel.canonical_conversation_id == control.lease.conversation_id,
                    )
                )
            elif kind == "effect" and key:
                valid = await reader.scalar(
                    select(effects.c.effect_key).where(
                        effects.c.effect_key == key,
                        effects.c.work_id == identity,
                        effects.c.state != "prepared",
                    )
                )
            elif kind == "child" and key:
                valid = await reader.scalar(
                    select(children.c.work_id).where(
                        children.c.work_id == key,
                        children.c.root_id == identity,
                        children.c.archived_at.is_(None),
                    )
                )
            elif kind == "artifact" and key:
                row = await reader.get(ToolArtifactModel, key)
                if (
                    row is not None
                    and not row.deleting
                    and await ToolArtifactRepository._authorized(
                        reader,
                        row,
                        control.context_access
                        or access_from_source(
                            control.lease.conversation_id, control.lease.generation, source
                        ),
                    )
                ):
                    valid = key
                    handles.append(key)
            if not valid:
                raise ValueError("work_context_note_invalid_reference")
    return note, tuple(handles), source_revision, privacy


async def publish_pending_note(control: WorkControl) -> str | None:
    """Retry only the saved publication intent, without invoking models or effects."""
    if control.current is None:
        return None
    saved = json.loads(control.current["checkpoint_json"]).get("context_note")
    if not isinstance(saved, dict):
        return None
    if await visible_context_note(control) is None:
        return None
    from qq_ai_bot.conversation.observations import ContextObservationRepository

    return await ContextObservationRepository(control.repository.database).publish_note(
        control.current, saved["revision"], saved["payload"], tuple(saved["artifact_handles"])
    )


async def visible_context_note(control: WorkControl) -> dict[str, Any] | None:
    """Select optional semantic payload using the current trusted read identity."""
    if control.current is None:
        return None
    saved = json.loads(control.current["checkpoint_json"]).get("context_note")
    if not isinstance(saved, dict):
        return None
    from dataclasses import asdict

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
    from qq_ai_bot.tool_results.access import access_from_source

    source = json.loads(control.current["source_json"])
    try:
        access = control.context_access or access_from_source(
            control.lease.conversation_id, control.lease.generation, control.source
        )
        payload = ContextNote.model_validate(saved.get("payload")).model_dump()
    except ValueError:
        return None
    if (
        source.get("actor_person_id", "") != access.actor_person_id
        or source.get("principal_kind", "person") != access.principal_kind
        or any(saved.get("access", {}).get(key) != value for key, value in asdict(access).items())
    ):
        return None
    async with control.repository.database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, access.conversation_id)
        privacy = int(
            await reader.scalar(
                select(ExecutionTraceStateModel.privacy_generation).where(
                    ExecutionTraceStateModel.id == 1
                )
            )
            or 0
        )
    if (
        conversation is None
        or conversation.generation != access.generation
        or saved.get("privacy_generation") != privacy
        or saved.get("access", {}).get("privacy_generation") != privacy
    ):
        return None
    return payload
