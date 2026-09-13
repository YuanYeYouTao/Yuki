"""Private bounded protocol checkpoints, separate from conversation evidence.

Opaque provider items retain their order and are never logged or fed into memory.
Media is stored separately by immutable content hash and hydrated on demand.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    FunctionCallOutput,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.runtime.subagent_schema import media, media_refs
from qq_ai_bot.runtime.work_media import externalize, hydrate, references
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.services.turn_transcript import TurnTranscript


def _continuation(value: dict[str, Any]) -> ProviderContinuation:
    payload = value["payload"]
    return ProviderContinuation(
        provider=value["provider"],
        protocol=value["protocol"],
        profile_id=value.get("profile_id", ""),
        payload=tuple(payload) if isinstance(payload, list) else payload,
    )


def encode_transcript(transcript: TurnTranscript) -> dict[str, Any]:
    request = transcript.request()
    items: list[dict[str, Any]] = []
    for item in (*request.messages, *request.items):
        items.append(
            {
                "kind": "message" if isinstance(item, ChatMessage) else "result",
                "value": asdict(item),
            }
        )
    return {
        "chain_id": transcript.chain_id,
        "messages_count": len(request.messages),
        "items": items,
        "continuation": asdict(request.continuation) if request.continuation else None,
    }


def decode_transcript(value: dict[str, Any]) -> TurnTranscript:
    items: list[ChatMessage | FunctionCallOutput] = []
    for encoded in value["items"]:
        data = dict(encoded["value"])
        if encoded["kind"] == "result":
            items.append(FunctionCallOutput(**data))
            continue
        data["images"] = tuple(ChatImage(**image) for image in data.get("images", ()))
        data["tool_calls"] = tuple(
            ToolCall(id=call["id"], type=call["type"], function=ToolFunction(**call["function"]))
            for call in data.get("tool_calls", ())
        )
        if data.get("response_item"):
            data["response_item"] = _continuation(data["response_item"])
        items.append(ChatMessage(**data))
    count = value["messages_count"]
    transcript = TurnTranscript(
        tuple(item for item in items[:count] if isinstance(item, ChatMessage))
    )
    transcript.chain_id = value["chain_id"]
    if value.get("continuation"):
        transcript.accept(_continuation(value["continuation"]))
    for item in items[count:]:
        if isinstance(item, ChatMessage):
            transcript.append(item)
        else:
            transcript.append_result(item.call_id, item.output)
    return transcript


class JournalUnavailable(RuntimeError):
    """A missing/corrupt checkpoint is not an authorized context reset."""


@dataclass(frozen=True)
class JournalSnapshot:
    reason: str
    record: dict[str, Any] | None = None
    previous_chain: str | None = None


class WorkJournal:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository

    async def load(self, lease: WorkLease, work_id: str, contract: str) -> JournalSnapshot:
        async with self.repository.database.sessions() as session:
            source = await session.get(CanonicalConversationModel, lease.conversation_id)
            row = (
                (await session.execute(select(journal).where(journal.c.work_id == work_id)))
                .mappings()
                .first()
            )
            if source is None or source.generation != lease.generation:
                raise WorkConflict("work_journal_generation_changed")
            if not row:
                used = await session.scalar(
                    select(work.c.model_requests).where(work.c.id == work_id)
                )
                if used:
                    raise JournalUnavailable("work_journal_missing")
                return JournalSnapshot("fresh")
            if row["contract"] != contract:
                return JournalSnapshot("contract_changed", previous_chain=row["chain_id"])
            if not lease.work_id and row["source_revision"] != source.prompt_source_revision:
                return JournalSnapshot("source_changed", previous_chain=row["chain_id"])
            result = dict(row)
            try:
                payload = json.loads(result["payload_json"])
            except ValueError as exc:
                raise JournalUnavailable("work_journal_corrupt") from exc
            refs = references(payload)
            if refs:
                blobs = {
                    str(item.sha256): bytes(item.content)
                    for item in (
                        await session.execute(select(media).where(media.c.sha256.in_(refs)))
                    ).all()
                }
                try:
                    result["payload_json"] = json.dumps(hydrate(payload, blobs), ensure_ascii=False)
                except (ValueError, KeyError) as exc:
                    raise JournalUnavailable("work_journal_media_missing") from exc
            return JournalSnapshot("resume", result, row["chain_id"])

    async def save(
        self,
        lease: WorkLease,
        work_id: str,
        contract: str,
        transcript: TurnTranscript,
        *,
        phase: str,
        pending: list[dict[str, Any]],
        source_revision: int,
        metadata: dict[str, Any],
    ) -> None:
        blobs: dict[str, bytes] = {}
        payload = bounded_json(
            externalize(
                {
                    "transcript": encode_transcript(transcript),
                    "pending": pending,
                    "metadata": metadata,
                },
                blobs,
            ),
            4 * 1024 * 1024,
        )
        async with self.repository.database.sessions() as session, session.begin():
            await self.repository._assert_lease(session, lease)
            source = await session.get(CanonicalConversationModel, lease.conversation_id)
            if source is None or source.generation != lease.generation:
                raise WorkConflict("work_journal_generation_changed")
            if not lease.work_id and source.prompt_source_revision != source_revision:
                raise WorkConflict("work_journal_source_changed")
            if phase == "response":
                from qq_ai_bot.runtime.work_recovery_schema import recovery

                await session.execute(
                    update(recovery)
                    .where(recovery.c.work_id == work_id)
                    .values(attempts=0, failure_json="{}", not_before=0)
                )
            await session.execute(
                update(work)
                .where(work.c.id == work_id)
                .values(
                    checkpoint_json=func.json_set(
                        work.c.checkpoint_json,
                        "$.execution_evidence",
                        func.json(bounded_json(metadata.get("effects", []), 65536)),
                    )
                )
            )
            previous_media = set(
                await session.scalars(
                    select(media_refs.c.sha256).where(media_refs.c.work_id == work_id)
                )
            )
            for digest, content in blobs.items():
                await session.execute(
                    insert(media).values(sha256=digest, content=content).on_conflict_do_nothing()
                )
            await session.execute(delete(media_refs).where(media_refs.c.work_id == work_id))
            for digest in blobs:
                await session.execute(insert(media_refs).values(work_id=work_id, sha256=digest))
            stale = previous_media - blobs.keys()
            if stale:
                await session.execute(
                    delete(media).where(
                        media.c.sha256.in_(stale),
                        media.c.sha256.not_in(select(media_refs.c.sha256)),
                    )
                )
            values = dict(
                work_id=work_id,
                chain_id=transcript.chain_id,
                contract=contract,
                source_revision=source.prompt_source_revision,
                phase=phase,
                payload_json=payload,
                updated=time.time(),
            )
            await session.execute(
                insert(journal)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[journal.c.work_id],
                    set_=values,
                )
            )

    async def invalidate(self, lease: WorkLease, work_id: str) -> None:
        async with self.repository.database.sessions() as session, session.begin():
            await self.repository._assert_lease(session, lease)
            await session.execute(delete(journal).where(journal.c.work_id == work_id))
            await session.execute(delete(media_refs).where(media_refs.c.work_id == work_id))
            await session.execute(
                delete(media).where(media.c.sha256.not_in(select(media_refs.c.sha256)))
            )

    async def recovered_inputs(self, lease: WorkLease, included: list[int], work_id: str) -> None:
        """The restored transcript contains staged inputs; consume without appending again."""
        async with self.repository.database.sessions() as session, session.begin():
            await self.repository._assert_lease(session, lease)
            await session.execute(
                update(inputs)
                .where(
                    inputs.c.conversation_id == lease.conversation_id,
                    inputs.c.generation == lease.generation,
                    inputs.c.work_id == work_id,
                    inputs.c.state == "staged",
                    inputs.c.id.in_(included),
                )
                .values(state="consumed")
            )
            await session.execute(
                update(inputs)
                .where(
                    inputs.c.conversation_id == lease.conversation_id,
                    inputs.c.generation == lease.generation,
                    inputs.c.work_id == work_id,
                    inputs.c.state == "staged",
                    inputs.c.id.not_in(included),
                )
                .values(state="pending", attempt_id=None)
            )

    async def effect_result(self, key: str) -> str:
        async with self.repository.database.sessions() as session:
            row = (
                (await session.execute(select(effects).where(effects.c.effect_key == key)))
                .mappings()
                .first()
            )
            if row is not None and row["state"] == "accepted":
                value = json.loads(row["receipt_json"])
                if isinstance(value.get("result"), str):
                    return str(value["result"])
                if value.get("transport_accepted"):
                    return json.dumps({"ok": True, "receipt": value, "replay_forbidden": True})
        if row is None or (
            row["state"] == "failed"
            and json.loads(row["receipt_json"]).get("error") == "never_dispatched"
        ):
            return json.dumps(
                {
                    "ok": False,
                    "executed": False,
                    "uncertain": False,
                    "error": "never_dispatched",
                    "replay_forbidden": True,
                }
            )
        from hashlib import sha256

        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        request_id = sha256(key.encode()).hexdigest()
        async with self.repository.database.sessions() as session:
            task = await session.get(SandboxTaskRunModel, request_id)
            if task is not None and task.run_id:
                completion = json.loads(task.completion_json or "{}")
                return json.dumps(
                    {
                        "ok": True,
                        "data": {
                            **completion,
                            "run_id": task.run_id,
                            "request_id": request_id,
                            "pending": task.status != "completed",
                            "detail": "已恢复原执行标识，查询该 run_id，不重新执行命令。",
                        },
                    },
                    ensure_ascii=False,
                )
        return json.dumps(
            {
                "ok": False,
                "error": "execution_outcome_unknown",
                "uncertain": True,
                "replay_forbidden": True,
                "detail": "核对原执行回执或产物；禁止再次执行原副作用。",
            },
            ensure_ascii=False,
        )
