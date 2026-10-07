"""Private bounded protocol checkpoints, separate from conversation evidence.

Opaque provider items retain their order and are never logged or fed into memory.
Media is stored separately by immutable content hash and hydrated on demand.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.capabilities.media import MediaResultText, result_images
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    FunctionCallOutput,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.subagent_schema import media, media_refs
from qq_ai_bot.runtime.work_media import externalize, hydrate, references
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.services.context_boundary import Publication
from qq_ai_bot.services.turn_transcript import TurnTranscript

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


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
    compaction_anchor: dict[str, Any] | None = None
    portable_search: tuple[dict[str, str], ...] = ()
    portable_search_truncated: bool = False
    pending_calls: tuple[dict[str, str], ...] = ()
    pending_sequence: int = 0
    task_material: dict[str, Any] | None = None
    # A delivery-only view survives a static contract change, without replaying
    # the old provider transcript, pending response or private continuation.
    delivery_record: dict[str, Any] | None = None


class WorkJournal:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository
        self.objects = ProtocolStore(repository.database)

    async def load(
        self,
        lease: WorkLease,
        work_id: str,
        contract: str,
        *,
        source_control: WorkControl | None = None,
    ) -> JournalSnapshot:
        self.objects.clear_record_cache()
        if source_control is not None and (
            source_control.lease != lease
            or source_control.current is None
            or source_control.current["id"] != work_id
        ):
            raise WorkConflict("work_journal_source_control_mismatch")
        loaded = await self._load(
            lease, work_id, contract, retain_source=source_control is not None
        )
        if (
            loaded.reason == "source_changed"
            and loaded.delivery_record
            and (loaded.record is None or source_control is None)
        ):
            raise WorkConflict("work_journal_source_changed")
        if loaded.reason != "source_changed" or loaded.record is None or source_control is None:
            return loaded
        # The read/file-hydration session above is closed before the guard opens
        # its own read snapshots. Never substitute newly assembled chat evidence.
        from dataclasses import replace

        from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard

        payload = json.loads(loaded.record["payload_json"])
        try:
            guard = WorkSourceGuard.restore(payload["metadata"]["source_guard"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalUnavailable("work_journal_corrupt") from exc
        if not await guard.check(source_control):
            if loaded.delivery_record:
                raise WorkConflict("work_journal_source_changed")
            return replace(loaded, record=None, compaction_anchor=None)
        if loaded.record["contract"] != contract:
            return replace(
                loaded,
                reason="contract_changed",
                record=None,
                compaction_anchor=payload["metadata"].get("compaction_anchor"),
            )
        return JournalSnapshot("resume", loaded.record, loaded.previous_chain)

    async def _load(
        self, lease: WorkLease, work_id: str, contract: str, *, retain_source: bool
    ) -> JournalSnapshot:
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
            source_changed = bool(
                not lease.work_id and row["source_revision"] != source.prompt_source_revision
            )
            result = dict(row)
            file_media: set[str] = set()
            payload: Any
            try:
                payload = await self.objects.hydrate(json.loads(result["payload_json"]))
                file_media = set(payload.get("file_media", []))
                result["payload_json"] = json.dumps(payload, ensure_ascii=False)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise JournalUnavailable("work_journal_corrupt") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("metadata", {}), dict):
                raise JournalUnavailable("work_journal_corrupt")
            delivery_record = None
            metadata = payload["metadata"]
            progress = metadata.get("progress", {})
            if (
                row["phase"] in {"delivery", "delivered"}
                and isinstance(progress, dict)
                and (progress.get("delivery_plan"))
            ):
                owner = (
                    await session.execute(
                        select(work.c.conversation_id, work.c.generation).where(
                            work.c.id == work_id
                        )
                    )
                ).one_or_none()
                if owner is None or tuple(owner) != (lease.conversation_id, lease.generation):
                    raise WorkConflict("work_journal_source_control_mismatch")
                origin = metadata.get(
                    "delivery_origin",
                    {
                        "work_id": work_id,
                        "chain_id": row["chain_id"],
                        "sequence": metadata.get("sequence", 0),
                    },
                )
                if (
                    not isinstance(origin, dict)
                    or set(origin) != {"work_id", "chain_id", "sequence"}
                    or origin.get("work_id") != work_id
                    or not isinstance(origin.get("chain_id"), str)
                    or not origin["chain_id"]
                    or len(origin["chain_id"]) > 36
                    or ":" in origin["chain_id"]
                    or type(origin.get("sequence")) is not int
                    or origin["sequence"] < 0
                ):
                    raise JournalUnavailable("work_journal_corrupt")
                delivery_record = {
                    "phase": row["phase"],
                    "origin": deepcopy(origin),
                    "plan": deepcopy(progress["delivery_plan"]),
                    "ending": metadata.get("ending"),
                    "event_ids": metadata.get("event_ids", []),
                    "source_keys": metadata.get("source_keys", []),
                    "input_ids": metadata.get("input_ids", []),
                    "source_guard": metadata.get("source_guard"),
                }
            contract_changed = row["contract"] != contract
            pending_calls: tuple[dict[str, str], ...] = ()
            pending_sequence = 0
            if contract_changed or source_changed:
                raw_pending = payload.get("pending", [])
                pending_sequence = payload["metadata"].get("sequence", 0)
                if (
                    not isinstance(raw_pending, list)
                    or type(pending_sequence) is not int
                    or pending_sequence < 0
                    or any(
                        not isinstance(call, dict)
                        or not isinstance(call.get("id"), str)
                        or not call["id"]
                        or not isinstance(call.get("name"), str)
                        or not call["name"]
                        or (
                            "readonly_result_key" in call
                            and (
                                not isinstance(call["readonly_result_key"], str)
                                or not call["readonly_result_key"]
                            )
                        )
                        for call in raw_pending
                    )
                ):
                    raise JournalUnavailable("work_journal_corrupt")
                pending_calls = tuple(
                    {
                        "id": call["id"],
                        "name": call["name"],
                        "arguments": call.get("arguments", ""),
                        **(
                            {"readonly_result_key": call["readonly_result_key"]}
                            if "readonly_result_key" in call
                            else {}
                        ),
                    }
                    for call in raw_pending
                )
            progress = payload["metadata"].get("progress", {})
            portable_search = (
                progress.get("portable_search", []) if isinstance(progress, dict) else []
            )
            if not isinstance(portable_search, list):
                portable_search = []
            portable_search = tuple(
                {
                    "url": item["url"],
                    "title": item.get("title", "")[:200],
                    "snippet": item.get("snippet", "")[:512],
                }
                for item in portable_search[:16]
                if isinstance(item, dict)
                and isinstance(item.get("url"), str)
                and isinstance(item.get("title", ""), str)
                and isinstance(item.get("snippet", ""), str)
            )
            retain_original = (
                source_changed and retain_source and bool(payload["metadata"].get("source_guard"))
            )
            if (contract_changed or source_changed) and not retain_original:
                payload = (
                    payload.get("metadata", {}).get("compaction_anchor")
                    if not source_changed
                    else None
                )
            try:
                refs = references(payload)
                if delivery_record is not None:
                    refs |= references(delivery_record)
                if any(not isinstance(digest, str) for digest in refs):
                    raise ValueError("invalid media reference")
            except (TypeError, ValueError) as exc:
                raise JournalUnavailable("work_journal_corrupt") from exc
            if refs:
                blobs = {
                    str(item.sha256): bytes(item.content)
                    for item in (
                        await session.execute(select(media).where(media.c.sha256.in_(refs)))
                    ).all()
                }
                try:
                    for digest in refs & file_media:
                        blobs[digest] = await self.objects.get_bytes(digest)
                except (OSError, ValueError) as exc:
                    raise JournalUnavailable("work_journal_media_missing") from exc
                try:
                    payload = hydrate(payload, blobs)
                    if delivery_record is not None:
                        delivery_record = hydrate(delivery_record, blobs)
                    result["payload_json"] = json.dumps(payload, ensure_ascii=False)
                except (ValueError, KeyError) as exc:
                    raise JournalUnavailable("work_journal_media_missing") from exc
            if source_changed:
                return JournalSnapshot(
                    "source_changed",
                    record=result if retain_original else None,
                    previous_chain=row["chain_id"],
                    pending_calls=pending_calls,
                    pending_sequence=pending_sequence,
                    portable_search=portable_search,
                    portable_search_truncated=bool(progress.get("portable_search_truncated", False))
                    if isinstance(progress, dict)
                    else False,
                    task_material=progress.get("task_material")
                    if isinstance(progress, dict)
                    else None,
                    delivery_record=delivery_record,
                )
            if contract_changed:
                return JournalSnapshot(
                    "contract_changed",
                    previous_chain=row["chain_id"],
                    compaction_anchor=payload,
                    portable_search=portable_search,
                    portable_search_truncated=bool(progress.get("portable_search_truncated", False))
                    if isinstance(progress, dict)
                    else False,
                    pending_calls=pending_calls,
                    pending_sequence=pending_sequence,
                    task_material=progress.get("task_material")
                    if isinstance(progress, dict)
                    else None,
                    delivery_record=delivery_record,
                )
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
        compaction_versions: tuple[int, int] | None = None,
        communication_updates: dict[str, Any] | None = None,
        publication: Publication | None = None,
    ) -> dict[str, Any] | None:
        communication_patch = (
            self.repository.encode_communication_updates(communication_updates)
            if communication_updates
            else None
        )
        updated_work = None
        await self.objects.refresh_policy()
        self.objects.begin_record_chain(work_id, transcript.chain_id)
        request = transcript.request()
        encoded_items: list[dict[str, Any]] = []
        item_digests: list[str | None] = []
        for record in (*request.messages, *request.items):
            if self.objects.cacheable_record(record):
                item_digests.append(await self.objects.put_record(record))
                encoded_items.append({})  # The existing manifest carries its verified digest.
            else:
                item_digests.append(None)
                encoded_items.append(ProtocolStore._encoded_record(record))
        encoded_transcript = {
            "chain_id": transcript.chain_id,
            "messages_count": len(request.messages),
            "items": encoded_items,
            "continuation": asdict(request.continuation) if request.continuation else None,
        }
        blobs: dict[str, bytes] = {}
        # Opaque Responses items retain insertion order all the way to the next
        # HTTP request; generic bounded_json sorts keys and changes that prefix.
        prepared = externalize(
            {
                "transcript": encoded_transcript,
                "pending": pending,
                "metadata": metadata,
            },
            blobs,
        )
        for digest, content in blobs.items():
            if await self.objects.put_bytes(content) != digest:
                raise ValueError("work_protocol_media_hash_mismatch")
        prepared["file_media"] = list(blobs)
        payload = json.dumps(
            await self.objects.manifest(
                prepared, refresh_policy=False, item_digests=tuple(item_digests)
            ),
            ensure_ascii=False,
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > 1024 * 1024:
            raise ValueError("work_record_too_large")
        # Reference extraction and JSON parsing happen before the writer begins.
        async with self.repository.database.sessions() as reader:
            input_media: set[str] = set()
            for value in await reader.scalars(
                select(inputs.c.payload_json).where(
                    inputs.c.conversation_id == lease.conversation_id,
                    inputs.c.generation == lease.generation,
                    inputs.c.work_id == work_id,
                    inputs.c.state.in_(("pending", "staged")),
                )
            ):
                input_media.update(references(json.loads(value)))
        next_media = input_media
        async with self.objects.publication(work_id) as prepared_objects:
            async with self.repository.database.immediate_session() as session:
                await self.repository._assert_lease(session, lease)
                source = await session.get(CanonicalConversationModel, lease.conversation_id)
                if source is None or source.generation != lease.generation:
                    raise WorkConflict("work_journal_generation_changed")
                if not lease.work_id and source.prompt_source_revision != source_revision:
                    raise WorkConflict("work_journal_source_changed")
                if compaction_versions is not None:
                    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

                    frozen_revision, frozen_privacy = compaction_versions
                    privacy = (
                        await session.scalar(
                            select(ExecutionTraceStateModel.privacy_generation).where(
                                ExecutionTraceStateModel.id == 1,
                            )
                        )
                        or 0
                    )
                    if (
                        source.prompt_source_revision != frozen_revision
                        or privacy != frozen_privacy
                    ):
                        raise WorkConflict("work_compaction_source_changed")
                if publication is not None:
                    await publication(session)
                if phase == "response":
                    from qq_ai_bot.runtime.work_recovery_schema import recovery

                    await session.execute(
                        update(recovery)
                        .where(recovery.c.work_id == work_id)
                        .values(attempts=0, failure_json="{}", not_before=0)
                    )
                previous_media = set(
                    await session.scalars(
                        select(media_refs.c.sha256).where(media_refs.c.work_id == work_id)
                    )
                )
                # Prepared inputs own their images until staged inputs are paired
                # into the journal. Saving the current transcript must retain them.
                added = tuple(sorted(next_media - previous_media))
                for offset in range(0, len(added), 256):
                    await session.execute(
                        insert(media_refs).on_conflict_do_nothing(),
                        [
                            {"work_id": work_id, "sha256": digest}
                            for digest in added[offset : offset + 256]
                        ],
                    )
                # Input append can race the prepare-reader. Keep active Work media
                # refs until archive/privacy cleanup; a paired save is not ownership.
                await self.objects.publish_refs(session, work_id, prepared_objects)
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
                if communication_patch is not None:
                    from qq_ai_bot.runtime.work_repository import TERMINAL

                    updated_work = (
                        (
                            await session.execute(
                                update(work)
                                .where(
                                    work.c.id == work_id,
                                    work.c.conversation_id == lease.conversation_id,
                                    work.c.generation == lease.generation,
                                    work.c.state.not_in(TERMINAL),
                                )
                                .values(
                                    checkpoint_json=func.json_patch(
                                        work.c.checkpoint_json, communication_patch
                                    ),
                                    updated=time.time(),
                                )
                                .returning(work)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if updated_work is None:
                        raise WorkConflict("work_checkpoint_obsolete")
        return dict(updated_work) if updated_work is not None else None

    async def invalidate(self, lease: WorkLease, work_id: str) -> None:
        async with self.repository.database.sessions() as session, session.begin():
            await self.repository._assert_lease(session, lease)
            from qq_ai_bot.runtime.protocol_schema import refs as protocol_refs

            await session.execute(delete(journal).where(journal.c.work_id == work_id))
            await session.execute(delete(protocol_refs).where(protocol_refs.c.work_id == work_id))
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
        def recorded_result(result: str) -> str:
            # This is the original persisted receipt reader, never a live adapter.
            from qq_ai_bot.runtime.effect_outcomes import (
                current_result_capture,
                historical_evidence,
            )

            capture = current_result_capture.get()
            if capture is not None:
                stored = json.loads(row["receipt_json"]) if row is not None else {}
                capture.evidence = historical_evidence(
                    stored
                    if row is not None and row["state"] == "accepted"
                    else {"result": result},
                    state="accepted",
                )
            return result

        async with self.repository.database.sessions() as session:
            row = (
                (await session.execute(select(effects).where(effects.c.effect_key == key)))
                .mappings()
                .first()
            )
        if row is not None and row["state"] == "accepted":
            value = json.loads(row["receipt_json"])
            if isinstance(value.get("result"), str):
                media_ref = value.get("media_result_ref")
                if media_ref is not None:
                    if not isinstance(media_ref, str):
                        raise JournalUnavailable("work_effect_media_corrupt")
                    try:
                        payload = await self.objects.get(media_ref)
                        blobs = {
                            digest: await self.objects.get_bytes(digest)
                            for digest in references(payload)
                        }
                        images = tuple(ChatImage(**image) for image in hydrate(payload, blobs))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise JournalUnavailable("work_effect_media_missing") from exc
                    return recorded_result(MediaResultText(value["result"], images))
                return recorded_result(str(value["result"]))
            if value.get("transport_accepted"):
                return recorded_result(
                    json.dumps({"ok": True, "receipt": value, "replay_forbidden": True})
                )
        if row is None or (
            row["state"] == "failed"
            and json.loads(row["receipt_json"]).get("error") == "never_dispatched"
        ):
            return recorded_result(
                json.dumps(
                    {
                        "ok": False,
                        "executed": False,
                        "uncertain": False,
                        "error": "never_dispatched",
                        "replay_forbidden": True,
                    }
                )
            )
        value = json.loads(row["receipt_json"])
        invocation = value.get("invocation", {})
        reference = invocation.get("original_domain_ref")
        if invocation.get("version") == 1 and isinstance(reference, str):
            from qq_ai_bot.runtime.effect_queries import RuntimeEffectQueries

            original = await RuntimeEffectQueries(
                self.repository.database
            ).inspect_social_operation(
                reference=reference, work_id=row["work_id"], operation_key=key
            )
            if original is not None:
                return recorded_result(
                    json.dumps(
                        {
                            "ok": original["status"] == "succeeded",
                            "data": original,
                            "original_domain_ref": reference,
                            "uncertain": original["status"] == "uncertain",
                            "work_effect_state": row["state"],
                            "replay_forbidden": True,
                        },
                        ensure_ascii=False,
                    )
                )
        from hashlib import sha256

        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        request_id = sha256(key.encode()).hexdigest()
        async with self.repository.database.sessions() as session:
            task = await session.get(SandboxTaskRunModel, request_id)
            if task is not None and task.run_id:
                completion = json.loads(task.completion_json or "{}")
                return recorded_result(
                    json.dumps(
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
                )
        return recorded_result(
            json.dumps(
                {
                    "ok": False,
                    "error": "execution_outcome_unknown",
                    "uncertain": True,
                    "replay_forbidden": True,
                    "detail": "核对原执行回执或产物；禁止再次执行原副作用。",
                },
                ensure_ascii=False,
            )
        )

    async def unsettled_composition(self, work_id: str | None, key: str) -> dict[str, Any] | None:
        """Only a recognized, still-open parent of this Work may resume its program."""
        if work_id is None:
            return None
        async with self.repository.database.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(effects.c.state, effects.c.receipt_json).where(
                            effects.c.effect_key == key,
                            effects.c.work_id == work_id,
                            effects.c.kind == "code_composition",
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["state"] not in {"prepared", "unknown"}:
            # Settled (partial/interrupted/completed) parents pair from their receipt.
            return None
        composition = json.loads(row["receipt_json"]).get("composition")
        if not isinstance(composition, dict) or composition.get("version") != 1:
            return None  # Unrecognized versions keep the conservative generic path.
        return {
            "operation_id": key,
            "snapshot_revision": int(composition.get("snapshot_revision", 0)),
            "snapshot_ref": composition.get("snapshot_ref"),
        }

    async def record_effect(
        self,
        key: str,
        state: str,
        receipt: dict[str, Any],
        *,
        media_source: tuple[str, int, int] | None = None,
    ) -> None:
        """Publish immutable pixels with the existing receipt, before its writer."""
        images = result_images(receipt.get("result"))
        if not images:
            await self.repository.record_effect(key, state, receipt)
            return
        async with self.repository.database.sessions() as reader:
            work_id = await reader.scalar(
                select(effects.c.work_id).where(effects.c.effect_key == key)
            )
        if work_id is None:
            raise WorkConflict("work_effect_missing")
        await self.objects.refresh_policy()
        blobs: dict[str, bytes] = {}
        prepared = externalize([asdict(image) for image in images], blobs)
        for digest, content in blobs.items():
            if await self.objects.put_bytes(content) != digest:
                raise JournalUnavailable("work_effect_media_corrupt")
        media_ref = await self.objects.put(prepared)
        private_receipt = {
            **receipt,
            "result": str(receipt["result"]),
            "media_result_ref": media_ref,
        }
        async with self.objects.publication(work_id) as prepared_protocol:
            await self.repository.record_effect(
                key,
                state,
                private_receipt,
                prepared_protocol=prepared_protocol,
                protocol_policy=self.objects.policy,
                media_source=media_source,
            )

    async def effect_state(self, key: str) -> str | None:
        """Read the original effect state without dispatching or changing it."""
        async with self.repository.database.sessions() as session:
            return await session.scalar(select(effects.c.state).where(effects.c.effect_key == key))
