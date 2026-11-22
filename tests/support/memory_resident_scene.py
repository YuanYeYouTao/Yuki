"""Shared real scene for the memory layering P1 (resident memory) E2E suites.

Task book: docs/architecture/Yuki-记忆分层P1实施任务书-2026-11-21.md §10. The common
fixture is Person A (the owner, superuser), B and C; groups G and H. A and B are
members of G, C is only in H. Facts are written through ``MemoryFactService`` with
evidence pointing at real chat events of the fixture conversations; turns run the
real ``MessageProcessor`` -> ``ChatService`` -> ``ContextAssembler`` ->
``MainAgentTurns.compose`` -> request assembly. Only the model reply is scripted.

Resident memory imports stay inside functions so this scene can be built (and its
non-resident parts smoke-tested) before the implementation lands.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, update
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.account_observations import apply_observations, profile_message
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from yuki.persistence.models import MemoryEvidenceModel, MemoryFactModel
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

BOT = "80001"
QQ = {"A": "10001", "B": "10002", "C": "1003", "B2": "1004", "D": "1005", "E": "1006", "F": "1007"}
GROUPS = {"G": "20001", "H": "20010001"}
RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-resident"
HEADER = "常驻记忆（"
_FACT_MARK = re.compile(r"（fact:(\d+)）")
# Section titles, fact lines (profile entries cite up to two IDs), profile name lines
# of group members (P4), profile topic lines and the explicit overflow line.
_SEGMENT_LINE = re.compile(
    r"^(\[[^\]]+\]|- .*（fact:\d+(?:,\d+)*）|· .*（person:[^）]*）"
    r"|- 可查话题：.+|（另有 \d+ 条.*）)$"
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SELECTION_ALIAS = re.compile(r"\b(kept|new|pinned)_\d+\b")
_DREAM_REF = re.compile(r"\bmemory_\d+\b")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?")

_RELATION = {
    MemoryAuthority.EXPLICIT: MemoryEvidenceRelation.EXPLICIT_COMMAND,
    MemoryAuthority.SELF_REPORT: MemoryEvidenceRelation.SELF_STATEMENT,
    MemoryAuthority.GROUP_REPORT: MemoryEvidenceRelation.GROUP_STATEMENT,
    MemoryAuthority.THIRD_PARTY: MemoryEvidenceRelation.THIRD_PARTY_STATEMENT,
    MemoryAuthority.AGENT_REFLECTION: MemoryEvidenceRelation.AGENT_REFLECTION,
}


def resident_segment(content: str) -> str:
    """The resident block as it appears in a request, read independently of the renderer.

    It starts at the header line and keeps the consecutive section, item and
    overflow lines (one fact per line, whitespace folded by the renderer).
    """
    lines = content.split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith(HEADER)]
    if not starts:
        return ""
    assert len(starts) == 1, "the resident segment must appear exactly once"
    kept = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if not _SEGMENT_LINE.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def fact_marks(segment: str) -> list[int]:
    return [int(value) for value in _FACT_MARK.findall(segment)]


def sections(segment: str) -> dict[str, list[int]]:
    """Section title -> rendered fact IDs in order."""
    result: dict[str, list[int]] = {}
    current = ""
    for line in segment.split("\n")[1:]:
        if line.startswith("["):
            current = line
            result[current] = []
        elif line.startswith("- "):
            result[current].extend(fact_marks(line))
    return result


@dataclass
class Turn:
    """One real Main turn: every model request it paid and its current message."""

    requests: list[Any]
    current: str
    segment: str
    fact_ids: list[int]
    result: Any = None


class ResidentScene:
    """Real SQLite + real Main; scripted model replies only."""

    def __init__(self, database, tmp_path) -> None:
        self.database = database
        self.tmp_path = tmp_path
        self.persons: dict[str, str] = {}
        self.spaces: dict[str, str] = {}
        self.conversations: dict[str, str] = {}
        self.events: dict[str, int] = {}
        self.facts: dict[str, int] = {}
        self.contents: dict[int, str] = {}
        self.records: dict[str, Any] = {}
        self.responder: Callable[[Any], Any] = lambda request: "NO_REPLY"
        self.selection_responder: Callable[[Any], Any] | None = None
        self.dream_responder: Callable[[Any], Any] | None = None
        self.extraction_responder: Callable[[Any], Any] | None = None
        self.selection_requests: list[Any] = []
        self.dream_requests: list[Any] = []
        self._sequence = 0

    # ------------------------------------------------------------------ setup
    async def build(self, *, work_enabled: bool = False, **settings: Any) -> ResidentScene:
        from tests.integration.test_execution_continuity_e2e import ScriptedProvider

        self.provider = ScriptedProvider(self._respond)
        self.env = await social_env(self.database, self.tmp_path)
        self.persons["A"] = self.env.person
        self.spaces["G"] = self.env.space
        async with self.database.immediate_session() as writer:
            for key in ("B", "C", "B2"):
                self.persons[key] = await ensure_person(
                    writer, QQ[key], display_name=f"person-{key}", now=datetime.now(UTC)
                )
            self.spaces["H"] = await ensure_space(writer, GROUPS["H"], name="group-H")
            # A is the owner: the only superuser in this scene.
            await writer.execute(update(CanonicalPersonModel).values(is_superuser=False))
            await writer.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id == self.persons["A"])
                .values(is_superuser=True)
            )
        for person, group in (("B", "G"), ("C", "H")):
            await apply_observations(
                self.database,
                profile_message(
                    user_id=QQ[person], nickname=f"person-{person}", group_id=GROUPS[group]
                ),
            )
        self.settings = make_settings(
            self.database.url,
            enabled_groups_csv=",".join(GROUPS.values()),
            runtime_work_enabled=work_enabled,
            automation_enabled=True,
            memory_dream_enabled=True,
            memory_embedding_enabled=False,
            **settings,
        )
        self.harness = build_harness(self.database, self.settings, self.provider)
        self.chat = self.harness.processor._chat
        self.short_state = ShortState(WorkspaceStore(self.tmp_path / "short-state"))
        self.chat.runtime.runner.main_contract = MainAgentContract(self.chat, self.short_state)
        self.chat.runtime.runner.code_mode_settings = self.settings
        self.chat._tools.short_state = self.short_state
        self.chat._tools.social_service = self.env.service
        self.env.service.runtime_config = self.chat._runtime_config
        self.harness.processor._rate_limiter = SlidingWindowRateLimiter(
            per_user=1000, per_group=1000
        )
        assert await self.env.router.cas_takeover_person(self.persons["A"]) == "taken"
        install_resident(self.chat, self.database)
        # Every fixture conversation exists before facts point at its events.
        for name, scope, sender in (
            ("private:A", ConversationScope.private(BOT, QQ["A"]), "A"),
            ("private:B", ConversationScope.private(BOT, QQ["B"]), "B"),
            ("private:C", ConversationScope.private(BOT, QQ["C"]), "C"),
            ("private:B2", ConversationScope.private(BOT, QQ["B2"]), "B2"),
            ("space:G", ConversationScope.group(BOT, GROUPS["G"]), "B"),
            ("space:H", ConversationScope.group(BOT, GROUPS["H"]), "C"),
        ):
            await self.event(name, sender, f"opening {name}", scope=scope)
        return self

    def _respond(self, request):
        """Route background model tasks to their scripts; everything else is Main."""
        text = request_text(request)
        if self.extraction_responder is not None and _is_extraction(request):
            return self.extraction_responder(request)
        if _SELECTION_ALIAS.search(text) and self.selection_responder is not None:
            self.selection_requests.append(request)
            return self.selection_responder(request)
        if _DREAM_REF.search(text) and self.dream_responder is not None:
            self.dream_requests.append(request)
            return self.dream_responder(request)
        return self.responder(request)

    async def runtime(self):
        return await self.chat._runtime_config.snapshot()

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    # ----------------------------------------------------------------- events
    async def event(
        self,
        conversation: str,
        sender: str,
        content: str,
        *,
        scope: ConversationScope | None = None,
        key: str | None = None,
    ) -> int:
        """Append one real accepted chat event; ``conversation`` is private:X / space:X."""
        if scope is None:
            kind, owner = conversation.split(":")
            scope = (
                ConversationScope.private(BOT, QQ[owner])
                if kind == "private"
                else ConversationScope.group(BOT, GROUPS[owner])
            )
        result = await seed_scoped_event(
            self.harness.scoped_events,
            scope=scope,
            origin_key=f"resident-{self._next()}",
            sender_user_id=QQ[sender],
            direction="inbound",
            content=content,
        )
        event = result.event
        self.conversations.setdefault(conversation, event.canonical_conversation_id)
        if key is not None:
            self.events[key] = event.id
            self.records[key] = event
        return event.id

    # ------------------------------------------------------------------ facts
    async def fact(
        self,
        name: str,
        owner: str,
        content: str,
        *,
        evidence: tuple[int, ...] = (),
        foreign_evidence: tuple[int, ...] = (),
        authority: MemoryAuthority = MemoryAuthority.SELF_REPORT,
        source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
        kind: MemoryKind = MemoryKind.FACT,
        visibility: str = "global",
        **fields: Any,
    ) -> int:
        """Write a fact through the real service.

        ``owner``: ``"A"`` (person), ``"B@G"`` (person_group), ``"G"`` (group) or
        ``"self"``; SELF ``visibility`` is ``global``, ``private:X`` or ``space:X``.

        ``evidence`` goes through the real write path and must all be accepted.
        ``foreign_evidence`` are events the real path refuses for this owner
        (``readable_evidence_predicate``: e.g. another person's private chat or another
        group); they are inserted directly, like the direct table writes of cases 3, 19
        and 28, to prove the render-time filters do not rely on write-time checks
        (legacy and imported rows exist in production).
        """
        target = self._target(owner, visibility)
        if owner in GROUPS and authority is MemoryAuthority.SELF_REPORT:
            authority = MemoryAuthority.GROUP_REPORT
        if owner == "self":
            authority = MemoryAuthority.AGENT_REFLECTION
        create = MemoryFactCreate(
            memory_key=f"resident:{name}",
            category="resident-e2e",
            content=content,
            source_type=source_type,
            authority=authority,
            kind=kind,
            importance=fields.pop("importance", 4),
            **target,
            **fields,
        )
        memories = self.chat._memories
        speaker = QQ.get(owner.split("@")[0], QQ["A"])

        def items_for(event_ids: tuple[int, ...]) -> list[MemoryEvidenceCreate]:
            return [
                MemoryEvidenceCreate(
                    event_id=event_id,
                    source_speaker_user_id=speaker,
                    relation=_RELATION[authority],
                    authority=authority,
                    excerpt=content[:40],
                )
                for event_id in event_ids
            ]

        items = items_for(evidence)
        created = await memories.remember(create, evidence=items[0] if items else None)
        if len(items) > 1:
            async with memories.write_session((created.id,)) as writer:
                await memories.append_evidence_bundle(
                    created.id, tuple(items[1:]), confirmed_at=datetime.now(UTC), session=writer
                )
        async with self.database.immediate_session() as writer:
            stored = set(
                await writer.scalars(
                    select(MemoryEvidenceModel.event_id).where(
                        MemoryEvidenceModel.fact_id == created.id
                    )
                )
            )
            assert stored == set(evidence), (name, sorted(stored), evidence)
            for item in items_for(foreign_evidence):
                writer.add(
                    MemoryEvidenceModel(
                        fact_id=created.id,
                        created_at=datetime.now(UTC),
                        **item.model_dump(mode="json"),
                    )
                )
        self.facts[name] = created.id
        self.contents[created.id] = created.content
        return created.id

    def _target(self, owner: str, visibility: str) -> dict[str, Any]:
        if owner == "self":
            if visibility == "global":
                return {
                    "scope_type": MemoryScopeType.SELF,
                    "visibility_type": SelfMemoryVisibility.GLOBAL,
                }
            kind, who = visibility.split(":")
            if kind == "private":
                return {
                    "scope_type": MemoryScopeType.SELF,
                    "visibility_type": SelfMemoryVisibility.PRIVATE,
                    "visibility_user_id": QQ[who],
                    "canonical_visibility_person_id": self.persons[who],
                }
            return {
                "scope_type": MemoryScopeType.SELF,
                "visibility_type": SelfMemoryVisibility.GROUP,
                "visibility_group_id": GROUPS[who],
                "canonical_visibility_space_id": self.spaces[who],
            }
        if owner in GROUPS:
            return {
                "scope_type": MemoryScopeType.GROUP,
                "group_id": GROUPS[owner],
                "canonical_subject_space_id": self.spaces[owner],
            }
        if "@" in owner:
            person, group = owner.split("@")
            return {
                "scope_type": MemoryScopeType.PERSON_GROUP,
                "subject_user_id": QQ[person],
                "group_id": GROUPS[group],
                "canonical_subject_person_id": self.persons[person],
                "canonical_subject_space_id": self.spaces[group],
            }
        return {
            "scope_type": MemoryScopeType.PERSON,
            "subject_user_id": QQ[owner],
            "canonical_subject_person_id": self.persons[owner],
        }

    async def set_fact(self, fact_id: int, **values: Any) -> None:
        """Fixture-only state edits (status, conflict, expiry) on an existing fact."""
        async with self.database.immediate_session() as writer:
            await writer.execute(
                update(MemoryFactModel).where(MemoryFactModel.id == fact_id).values(**values)
            )

    # --------------------------------------------------------------- resident
    def owner(self, key: str):
        from yuki.memory.resident import ResidentOwner

        if key == "self":
            return ResidentOwner(kind="self")
        if key in GROUPS:
            return ResidentOwner(kind="space", space_id=self.spaces[key])
        return ResidentOwner(kind="person", person_id=self.persons[key])

    def audience(self, key: str):
        from yuki.memory.resident import ResidentAudience

        if key in GROUPS:
            return ResidentAudience(kind="space", person_id=None, space_id=self.spaces[key])
        return ResidentAudience(kind="private", person_id=self.persons[key], space_id=None)

    @property
    def resident(self):
        from yuki.memory.resident import ResidentMemory

        return ResidentMemory(self.database)

    async def write_set(self, key: str, fact_ids: list[int], *, source: str = "owner") -> None:
        await self.resident.write_set(self.owner(key), list(fact_ids), source=source)

    async def raw_set(self, key: str, fact_ids: list[int], *, source: str = "dream") -> None:
        """Bypass every write-time check: proves render-time filters stand alone."""
        from yuki.persistence.models import MemoryResidentSetModel

        owner = self.owner(key)
        now = datetime.now(UTC)
        async with self.database.immediate_session() as writer:
            row = await writer.scalar(
                select(MemoryResidentSetModel).where(
                    MemoryResidentSetModel.owner_kind == owner.kind,
                    MemoryResidentSetModel.owner_person_id.is_(owner.person_id)
                    if owner.person_id is None
                    else MemoryResidentSetModel.owner_person_id == owner.person_id,
                    MemoryResidentSetModel.owner_space_id.is_(owner.space_id)
                    if owner.space_id is None
                    else MemoryResidentSetModel.owner_space_id == owner.space_id,
                )
            )
            if row is None:
                row = MemoryResidentSetModel(
                    owner_kind=owner.kind,
                    owner_person_id=owner.person_id,
                    owner_space_id=owner.space_id,
                    source="owner" if owner.kind == "self" else source,
                    considered_through_fact_id=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
                writer.add(row)
            row.fact_ids_json = json.dumps(list(fact_ids))
            row.revision = (row.revision or 0) + 1
            row.updated_at = now

    async def table(self) -> list[dict[str, Any]]:
        """``memory_resident_sets`` rows with owner IDs replaced by fixture labels."""
        from yuki.persistence.models import MemoryResidentSetModel

        labels = self.labels()
        async with self.database.read_session() as reader:
            rows = (
                await reader.scalars(
                    select(MemoryResidentSetModel).order_by(
                        MemoryResidentSetModel.owner_kind,
                        MemoryResidentSetModel.owner_person_id,
                        MemoryResidentSetModel.owner_space_id,
                    )
                )
            ).all()
        return sorted(
            (
                {
                    "owner_kind": row.owner_kind,
                    "owner": labels.get(row.owner_person_id or row.owner_space_id or "", "self"),
                    "fact_ids": json.loads(row.fact_ids_json),
                    "source": row.source,
                    "considered_through_fact_id": row.considered_through_fact_id,
                    "revision": row.revision,
                }
                for row in rows
            ),
            key=lambda item: (item["owner_kind"], item["owner"]),
        )

    # ------------------------------------------------------------------ turns
    def message(
        self, who: str, text: str, *, group: str | None = None, person: str | None = None
    ) -> InboundMessage:
        key = f"resident-turn-{self._next()}"
        message = InboundMessage(
            ingress_key=key,
            native_message_id=key,
            event_type="message:test",
            scope_type=ScopeType.GROUP if group else ScopeType.PRIVATE,
            sender=SenderIdentity(QQ[who], nickname=f"person-{who}"),
            text=text,
            bot_user_id=BOT,
            group_id=GROUPS[group] if group else None,
            mentions_bot=bool(group),
            person_id=self.persons[person or who],
            presence_id=self.env.presence,
        )
        if group:
            message = replace(message, space_id=self.spaces[group])
        return message

    async def command(self, who: str, text: str, *, person: str | None = None) -> list[str]:
        """A real private ``/ai`` command; returns the texts sent back."""
        sender = MemorySender()
        await self.harness.processor.handle(self.message(who, text, person=person), sender)
        return [str(message.text) for message in sender.messages]

    async def turn(
        self, who: str, text: str, *, group: str | None = None, person: str | None = None
    ) -> Turn:
        start = len(self.provider.requests)
        result = await self.harness.processor.handle(
            self.message(who, text, group=group, person=person), MemorySender()
        )
        assert result.reason == "chat", result
        requests = [
            request
            for request in self.provider.requests[start:]
            if text in str(request.messages[-1].content or "")
        ]
        assert requests, "the turn paid no Main request carrying its message"
        return capture(requests, result)

    async def contract(self) -> str:
        runtime = await self.runtime()
        contract = await self.chat.runtime.main_turns.recovery_contract(runtime)
        assert contract
        return contract

    async def tool_turn(
        self,
        who: str,
        text: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        group: str | None = None,
        person: str | None = None,
    ) -> tuple[list[Any], Turn]:
        """One real turn whose scripted model issues ``calls`` one per request."""
        identities = [f"resident-call-{self._next()}" for _ in calls]
        replies: list[Any] = []
        state = {"index": 0}

        def respond(request):
            index = state["index"]
            if index:
                replies.append(tool_result(request, identities[index - 1]))
            state["index"] += 1
            if index >= len(calls):
                return "NO_REPLY"
            name, arguments = calls[index]
            return tool_call(name, arguments, identities[index])

        previous, self.responder = self.responder, respond
        try:
            turn = await self.turn(who, text, group=group, person=person)
        finally:
            self.responder = previous
        assert len(replies) == len(calls), replies
        return replies, turn

    # ------------------------------------------------------------- extraction
    async def extract(self, event_key: str, claims: list[dict[str, Any]]) -> int:
        """Run the real batch extraction worker on one fixture event."""
        from yuki.memory.extraction import BatchMemoryClaim, BatchMemoryExtractionOutput

        def respond(request):
            payload = json.loads(request.messages[-1].content)
            return BatchMemoryExtractionOutput(
                claims=tuple(
                    BatchMemoryClaim.model_validate(
                        {
                            "source_event_id": next(
                                item["source_event_id"]
                                for item in payload["events"]
                                if claim["evidence_quote"] in item["content"]
                            ),
                            "claim": claim,
                        }
                    )
                    for claim in claims
                )
            ).model_dump_json()

        event = self.records[event_key]
        worker = self.harness.processor._memory_worker
        self.extraction_responder = respond
        try:
            await worker.enqueue(event.id, event.scope.key)
            return await worker.process_once()
        finally:
            self.extraction_responder = None

    async def fact_ids_after(self, fact_id: int) -> list[int]:
        async with self.database.read_session() as reader:
            return list(
                await reader.scalars(
                    select(MemoryFactModel.id)
                    .where(MemoryFactModel.id > fact_id)
                    .order_by(MemoryFactModel.id)
                )
            )

    # ------------------------------------------------------- dream/selection
    def dream(self):
        """Real Dream service, repository and worker over the scene database."""
        if hasattr(self, "_dream"):
            return self._dream
        from tests.support.model_executor import InjectedModelExecutor

        from yuki.memory.dream.repository import DreamRepository
        from yuki.memory.dream.service import DreamService
        from yuki.memory.embedding.fake import FakeEmbeddingProvider
        from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
        from yuki.memory.fts import SQLiteMemoryFTSIndex
        from yuki.memory.retrieval import MemoryRetriever
        from yuki.services.concurrency import ConcurrencyManager

        memories = self.chat._memories
        repository = DreamRepository(self.database)
        models = InjectedModelExecutor(self.provider)
        service = DreamService(
            settings=self.settings,
            repository=repository,
            facts=memories,
            mutations=self.harness_mutations(),
            embeddings=MemoryEmbeddingRuntime(
                settings=self.settings,
                database=self.database,
                facts=memories,
                retriever=MemoryRetriever(
                    repository=memories.repository,
                    lexical_index=SQLiteMemoryFTSIndex(self.database),
                ),
                provider=FakeEmbeddingProvider(),
            ),
            models=models,
            concurrency=ConcurrencyManager(),
        )
        selection = build_selection(self, models)
        worker = build_dream_worker(self, repository, service, selection)
        self._dream = SimpleNamespace(
            repository=repository, service=service, worker=worker, selection=selection
        )
        return self._dream

    def harness_mutations(self):
        from yuki.memory.claim_processor import MemoryClaimProcessor
        from yuki.memory.mutation.service import MemoryMutationService

        memories = self.chat._memories
        return MemoryMutationService(
            settings=self.settings,
            facts=memories,
            processor=MemoryClaimProcessor(settings=self.settings, facts=memories),
            ledger=self.harness.ledger,
        )

    async def dream_run(self, clusters: tuple[tuple[str, ...], ...] = ()) -> Any:
        """Create, start and drain one Dream run through the real worker loop body.

        ``clusters`` are tuples of fact names; each becomes one explicit cluster so
        the scripted Dream reply can address ``memory_1``.. deterministically.
        """
        from yuki.memory.dream.models import DreamPlanStatistics, DreamRunMode

        dream = self.dream()
        memories = self.chat._memories
        specs = []
        for index, names in enumerate(clusters):
            rows = [await memories.get_fact(self.facts[name]) for name in names]
            specs.append(
                (
                    f"resident-cluster-{index}",
                    "partition",
                    BOT,
                    rows[0].kind.value,
                    tuple(row.id for row in rows),
                    dream.service._cluster_fingerprint(tuple(rows)),
                )
            )
        latest = await self.fact_ids_after(0)
        run = await dream.repository.create_run(
            mode=DreamRunMode.FULL,
            statistics=DreamPlanStatistics(
                eligible_facts=0,
                ready_facts=0,
                missing_embeddings=0,
                ambiguous_bot_facts=0,
                partitions=0,
                candidate_clusters=len(specs),
                isolated_facts=0,
                estimated_model_calls=len(specs),
            ),
            clusters=tuple(specs),
            snapshot_max_fact_id=max(latest, default=0),
            actor_user_id=QQ["A"],
            scheduled_slot=None,
        )
        assert await dream.repository.start_run(run.public_id)
        await dream.worker._drain_active()
        return await dream.repository.get_run(run.public_id)

    async def contents_of(self, fact_ids: list[int]) -> dict[int, str]:
        async with self.database.read_session() as reader:
            rows = await reader.execute(
                select(MemoryFactModel.id, MemoryFactModel.content).where(
                    MemoryFactModel.id.in_(fact_ids)
                )
            )
            return {row.id: row.content for row in rows}

    async def reordered_copy(self, path: Path, *, reverse: bool) -> str:
        """A copy of the database whose resident rows were re-inserted in another order."""
        import asyncio
        import shutil
        import sqlite3

        async with self.database.engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        source = Path(self.database.url.removeprefix("sqlite+aiosqlite:///"))

        def rebuild() -> None:
            shutil.copyfile(source, path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                for table in ("memory_evidence", "memory_resident_sets", "memory_facts"):
                    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY id")
                    columns = [item[0] for item in cursor.description]
                    rows = cursor.fetchall()
                    connection.execute(f"DELETE FROM {table}")
                    placeholders = ",".join("?" for _ in columns)
                    for row in reversed(rows) if reverse else rows:
                        connection.execute(
                            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                            row,
                        )
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(rebuild)
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    # -------------------------------------------------------------- artifacts
    def labels(self) -> dict[str, str]:
        labels = {value: f"person:{key}" for key, value in self.persons.items()}
        labels.update({value: f"space:{key}" for key, value in self.spaces.items()})
        labels.update({value: f"conversation:{key}" for key, value in self.conversations.items()})
        return labels

    def write(self, case: str, payload: dict[str, Any]) -> Path:
        return write_artifact(case, payload, self.labels())


def system_text(request: Any) -> str:
    """The system message: the stable prefix whose last part is the resident segment."""
    return "\n".join(
        str(message.content or "") for message in request.messages if message.role == "system"
    )


def capture(requests: list[Any], result: Any = None) -> Turn:
    current = str(requests[0].messages[-1].content or "")
    # The resident segment lives at the end of the system message, never in the
    # current message (stable prefix placement).
    assert HEADER not in current, "the resident segment must not be in the current message"
    segment = resident_segment(system_text(requests[0]))
    return Turn(
        requests=requests,
        current=current,
        segment=segment,
        fact_ids=fact_marks(segment),
        result=result,
    )


def install_resident(chat, database) -> None:
    """Give the real ChatService a ContextAssembler that owns a ResidentMemory.

    The container wires one ``ResidentMemory`` into ``ContextAssembler``; the shared
    test harness predates it, so the scene rebuilds the assembler with the same
    ledger and scope repository.
    """
    from yuki.memory.resident import ResidentMemory
    from yuki.services.context_assembler import ContextAssembler

    chat._context_assembler = ContextAssembler(
        settings=chat._settings,
        ledger=chat._ledger,
        conversation_scopes=chat._conversation_scopes,
        resident=ResidentMemory(database),
    )


def build_selection(scene: ResidentScene, models: Any):
    """The resident selection step as ``ConversationModule`` wires it (MEMORY_DREAM route)."""
    from yuki.memory.resident import ResidentMemory
    from yuki.memory.resident_selection import ResidentSelection
    from yuki.services.concurrency import ConcurrencyManager

    return ResidentSelection(
        settings=scene.settings,
        database=scene.database,
        resident=ResidentMemory(scene.database),
        models=models,
        concurrency=ConcurrencyManager(),
    )


def build_dream_worker(scene: ResidentScene, repository: Any, service: Any, selection: Any):
    """The Dream worker with the selection step wired in (§15 #22)."""
    from yuki.memory.dream.worker import DreamWorker

    return DreamWorker(
        settings=scene.settings,
        repository=repository,
        service=service,
        resident=selection,
    )


def _refs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        found = [value] if "ref" in value and "content" in value else []
        return found + [item for child in value.values() for item in _refs(child)]
    if isinstance(value, list):
        return [item for child in value for item in _refs(child)]
    return []


def selection_aliases(request, contents: dict[int, str]) -> dict[str, int]:
    """Map each alias (``kept_N``/``new_N``/``pinned_N``) offered in a selection request
    to the fixture fact with the same (whitespace-folded) text."""
    by_text = {" ".join(content.split()): fact_id for fact_id, content in contents.items()}
    result: dict[str, int] = {}
    for message in request.messages:
        try:
            payload = json.loads(message.content or "")
        except ValueError:
            continue
        for row in _refs(payload):
            fact_id = by_text.get(" ".join(str(row["content"]).split()))
            if fact_id is not None and _SELECTION_ALIAS.fullmatch(str(row["ref"])):
                result[str(row["ref"])] = fact_id
    return result


def selection_reply(aliases: list[str]) -> str:
    """Alias-only structured output of the selection model (never database IDs)."""
    return json.dumps({"selected": list(aliases)}, ensure_ascii=False)


def normalize(value: Any, labels: dict[str, str]) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    for raw, label in sorted(labels.items(), key=lambda item: -len(item[0])):
        if raw:
            text = text.replace(raw, f"<{label}>")
    seen: dict[str, str] = {}
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid:{len(seen) + 1}>"), text)
    return _ISO.sub("<time>", text)


def write_artifact(case: str, payload: dict[str, Any], labels: dict[str, str]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{case}.json"
    path.write_text(normalize(payload, labels) + "\n", encoding="utf-8")
    return path


def turn_record(turn: Turn, contract: str) -> dict[str, Any]:
    """``recovery_contract()`` already returns a sha256 hex digest."""
    return {"segment": turn.segment, "fact_ids": turn.fact_ids, "recovery_contract": contract}


def request_text(request) -> str:
    return "\n".join(str(message.content or "") for message in request.messages)


def _is_extraction(request) -> bool:
    try:
        payload = json.loads(request.messages[-1].content or "")
    except ValueError:
        return False
    return isinstance(payload, dict) and "events" in payload


def tool_result(request, call_id: str) -> Any:
    message = next(
        message
        for message in reversed(request.messages)
        if message.role == "tool" and message.tool_call_id == call_id
    )
    return json.loads(message.content or "null")


def tool_call(name: str, arguments: dict[str, Any], call_id: str):
    from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction

    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


async def run_cli(database_url: str, *arguments: str) -> SimpleNamespace:
    """Run the real ``yuki`` CLI in a child process against a scene database URL."""
    import asyncio
    import os

    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "LLM_PROVIDER": "fake",
        "LLM_MODEL": "fake-model",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from yuki.cli import main; main()",
        *arguments,
        "--database-url",
        database_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await process.communicate()
    return SimpleNamespace(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )
