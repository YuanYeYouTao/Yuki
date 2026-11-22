"""Owner tooling for person profiles (layered memory P4 §9, #227).

Contract: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md §5, §8.3, §8.4, §9.
Everything here backs ``yuki memory profile``: the latest member window and
maintenance set (§5.1, §5.2), the offline first-version generation (§8.3) written to a
plan file instead of the database, applying a reviewed plan (§8.4 checks plus the
signature and version recorded at generation), rollback, version diffs, the §7.4
check of every stored profile and the status summary.

Writes go only through ``ProfileStore``; reads never hold a write transaction, and the
model call runs before any write. The window, the maintenance set, what is due and the
model call are the Dream selection step's own (``memory.profile_selection``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from yuki.config import Settings
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.memory.dream.db_models import MemoryDreamRunModel
from yuki.memory.profile_selection import (
    FINISHED_RUN_STATUSES,
    ProfileMaintenance,
    ProfileRejected,
    dream_window,
    due_profiles,
    window_activity,
)
from yuki.memory.profiles import (
    MAX_GROUP_MEMBER_ENTRIES,
    EntryCheck,
    Profile,
    ProfileAudience,
    ProfileCommit,
    ProfileConflict,
    ProfileEntry,
    ProfileStore,
    ProfileValidationError,
    RenderedProfile,
    person_root,
    validate_entries,
)
from yuki.memory.resident import ResidentMemory
from yuki.model_runtime.executor import ModelExecutor
from yuki.model_runtime.structured import StructuredTaskRunner
from yuki.persistence.database import Database
from yuki.services.concurrency import ConcurrencyManager

# ---------------------------------------------------------------- window (§5)


@dataclass(frozen=True, slots=True)
class ProfileWindow:
    """The window of the latest finished Dream run and who it maintains (§5.1, §5.2).

    ``spaces``: each group's displayed people in order: the owner list when pinned,
    else the window ranking (top five by inbound messages, ties by root ID), else the
    stored list of a group that was quiet in the window (P4-Q4). ``private``: roots
    with an inbound private message in the window.
    """

    start: datetime
    end: datetime
    dream_run_public_id: str | None
    spaces: dict[str, tuple[str, ...]]
    private: tuple[str, ...]

    @property
    def public(self) -> tuple[str, ...]:
        """Public profiles are maintained for every listed person and private chatter."""
        listed = {root for roots in self.spaces.values() for root in roots}
        return tuple(sorted(listed | set(self.private)))


async def latest_window(database: Database) -> ProfileWindow:
    """The latest finished Dream run's window and lists (the selection step's own
    ``dream_window`` / ``window_activity``); never writes."""
    run = MemoryDreamRunModel
    async with database.read_session() as session:
        latest = await session.scalar(
            select(run.public_id)
            .where(run.status.in_(FINISHED_RUN_STATUSES), run.started_at.is_not(None))
            .order_by(run.started_at.desc(), run.id.desc())
            .limit(1)
        )
    window = (
        await dream_window(database, latest) if latest is not None else await dream_window(database)
    )
    activity = await window_activity(database, window)
    spaces = dict(activity.ranking)
    for members in await ProfileStore(database).all_members():
        if members.source == "owner" or members.space_id not in spaces:
            spaces[members.space_id] = members.person_ids
    return ProfileWindow(
        start=window.start,
        end=window.end,
        dream_run_public_id=latest,
        spaces=dict(sorted(spaces.items())),
        private=tuple(sorted(activity.private_roots)),
    )


# ---------------------------------------------------------- generation (§8.3)


@dataclass(frozen=True, slots=True)
class PlannedProfile:
    """One generated profile: ``entries`` starts equal to ``generated``; the owner may
    edit ``entries`` (or ``reason``) before ``apply``."""

    person_id: str  # root
    audience: ProfileAudience
    base_version: int
    signature: str  # available-fact signature at generation
    reason: str
    generated: tuple[ProfileEntry, ...]
    entries: tuple[ProfileEntry, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "audience": self.audience,
            "base_version": self.base_version,
            "signature": self.signature,
            "reason": self.reason,
            "generated": [entry.to_json() for entry in self.generated],
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> PlannedProfile:
        audience: ProfileAudience = "private" if value["audience"] == "private" else "public"
        if value["audience"] != audience:
            raise ProfileValidationError("unknown_audience", str(value["audience"]))
        return cls(
            person_id=str(value["person_id"]),
            audience=audience,
            base_version=int(value["base_version"]),
            signature=str(value["signature"]),
            reason=str(value["reason"]),
            generated=tuple(ProfileEntry.from_json(item) for item in value["generated"]),
            entries=tuple(ProfileEntry.from_json(item) for item in value["entries"]),
        )


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    generated_at: datetime
    profiles: tuple[PlannedProfile, ...]

    def write(self, path: Path) -> None:
        payload = {
            "generated_at": self.generated_at.isoformat(),
            "profiles": [item.to_json() for item in self.profiles],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")

    @classmethod
    def read(cls, path: Path) -> ProfilePlan:
        payload = json.loads(path.read_text("utf-8"))
        return cls(
            generated_at=datetime.fromisoformat(payload["generated_at"]),
            profiles=tuple(PlannedProfile.from_json(item) for item in payload["profiles"]),
        )


@dataclass(frozen=True, slots=True)
class ProfileOutcome:
    """A profile that was skipped, rejected or applied, with the reason code."""

    person_id: str
    audience: ProfileAudience
    code: str
    fact_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationResult:
    plan: ProfilePlan
    skipped: tuple[ProfileOutcome, ...]  # no available facts: no model call
    rejected: tuple[ProfileOutcome, ...]  # the call's output was void (§8.4)
    model_calls: int


def displayed_targets(window: ProfileWindow) -> tuple[tuple[str, ProfileAudience], ...]:
    """The maintenance set as profiles: per person, public before private (§8.2)."""
    private = set(window.private)
    targets: list[tuple[str, ProfileAudience]] = []
    for root in window.public:
        targets.append((root, "public"))
        if root in private:
            targets.append((root, "private"))
    return tuple(targets)


class ProfileGeneration:
    """The offline first version (§15.1): the Dream step's ``propose`` per profile,
    results go to a plan, never the database."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        models: ModelExecutor,
        concurrency: ConcurrencyManager,
    ) -> None:
        self._maintenance = ProfileMaintenance(
            settings=settings,
            database=database,
            store=ProfileStore(database),
            structured=StructuredTaskRunner(models),
            concurrency=concurrency,
        )

    async def generate(self, targets: Sequence[tuple[str, ProfileAudience]]) -> GenerationResult:
        planned: list[PlannedProfile] = []
        skipped: list[ProfileOutcome] = []
        rejected: list[ProfileOutcome] = []
        calls = 0
        for person_id, audience in targets:
            public = next(
                (
                    item.entries
                    for item in planned
                    if (item.person_id, item.audience) == (person_id, "public")
                ),
                None,
            )
            try:
                proposal = await self._maintenance.propose(
                    person_id, audience, public_entries=public
                )
            except ProfileRejected as exc:
                calls += exc.model_calls
                rejected.append(ProfileOutcome(person_id, audience, exc.code))
                continue
            if proposal is None:
                skipped.append(ProfileOutcome(person_id, audience, "no_available_facts"))
                continue
            calls += proposal.model_calls
            planned.append(
                PlannedProfile(
                    person_id=proposal.person_id,
                    audience=audience,
                    base_version=proposal.base_version,
                    signature=proposal.signature,
                    reason=proposal.reason,
                    generated=proposal.entries,
                    entries=proposal.entries,
                )
            )
        return GenerationResult(
            ProfilePlan(datetime.now(UTC), tuple(planned)), tuple(skipped), tuple(rejected), calls
        )


# --------------------------------------------------------------- apply (§9)


@dataclass(frozen=True, slots=True)
class AppliedProfile:
    person_id: str
    audience: ProfileAudience
    version: int
    written: bool  # False: the entries equalled the current ones
    edited: bool  # the owner changed the generated entries
    locked: bool


async def apply_plan(
    store: ProfileStore, plan: ProfilePlan
) -> tuple[tuple[AppliedProfile, ...], tuple[ProfileOutcome, ...]]:
    """Write each planned profile whose current version and available-fact signature
    still equal the plan's; the rest are rejected one by one. A profile the owner
    edited is locked; an unedited one keeps its lock state."""
    applied: list[AppliedProfile] = []
    rejected: list[ProfileOutcome] = []
    for item in plan.profiles:
        person, audience = item.person_id, item.audience
        try:
            current = await store.load(person, audience)
            version = current.current_version if current is not None else 0
            if version != item.base_version:
                raise ProfileConflict("version_changed")
            if (await store.available(person, audience)).signature != item.signature:
                rejected.append(ProfileOutcome(person, audience, "signature_changed"))
                continue
            entries = validate_entries(item.entries)
            check = await store.check(person, audience, entries)
            if check.dropped:
                failed = tuple(fact for dropped in check.dropped for fact in dropped.fact_ids)
                rejected.append(ProfileOutcome(person, audience, "unavailable_fact", failed))
                continue
            edited = entries != validate_entries(item.generated)
            commit = await store.commit(
                person,
                audience,
                entries,
                source="owner",
                reason=f"owner apply{' (edited)' if edited else ''}: {item.reason}",
                expected_version=version,
                signature=item.signature,
                lock=True if edited else None,
            )
        except (ProfileConflict, ProfileValidationError) as exc:
            rejected.append(ProfileOutcome(person, audience, exc.code))
            continue
        applied.append(
            AppliedProfile(
                person_id=commit.profile.person_id,
                audience=audience,
                version=commit.profile.current_version,
                written=commit.written,
                edited=edited,
                locked=commit.profile.locked,
            )
        )
    return tuple(applied), tuple(rejected)


# ------------------------------------------------------ rollback, diff, check


@dataclass(frozen=True, slots=True)
class RollbackResult:
    version: int  # the version rolled back to
    entries: tuple[ProfileEntry, ...]  # that version's entries before the check
    commit: ProfileCommit
    check: EntryCheck


async def rollback(
    store: ProfileStore, person_id: str, audience: ProfileAudience, version: int
) -> RollbackResult:
    """A new owner version from version N's entries after the §7.4 check; locks."""
    target = await store.load_version(person_id, audience, version)
    current = await store.load(person_id, audience)
    if target is None or current is None:
        raise ProfileValidationError("unknown_version", str(version))
    check = await store.check(person_id, audience, target.entries)
    commit = await store.commit(
        person_id,
        audience,
        check.entries,
        source="owner",
        reason=f"owner rollback to v{version}",
        expected_version=current.current_version,
        lock=True,
    )
    return RollbackResult(version, target.entries, commit, check)


@dataclass(frozen=True, slots=True)
class EntryDiff:
    added: tuple[ProfileEntry, ...]
    removed: tuple[ProfileEntry, ...]
    rewritten: tuple[tuple[ProfileEntry, ProfileEntry], ...]  # (old, new) sharing a fact
    unchanged: int
    reordered: bool  # the unchanged entries appear in another order


def diff_entries(old: Sequence[ProfileEntry], new: Sequence[ProfileEntry]) -> EntryDiff:
    """Equal entries are unchanged; an unmatched new entry that shares a fact ID with an
    unmatched old one rewrote it; the rest were added or removed."""
    remaining = list(old)
    kept: list[ProfileEntry] = []
    unmatched: list[ProfileEntry] = []
    for entry in new:
        if entry in remaining:
            remaining.remove(entry)
            kept.append(entry)
        else:
            unmatched.append(entry)
    rewritten: list[tuple[ProfileEntry, ProfileEntry]] = []
    added: list[ProfileEntry] = []
    for entry in unmatched:
        partner = next(
            (item for item in remaining if set(item.fact_ids) & set(entry.fact_ids)), None
        )
        if partner is None:
            added.append(entry)
        else:
            remaining.remove(partner)
            rewritten.append((partner, entry))
    kept_in_old = [entry for entry in old if entry in kept]
    return EntryDiff(
        added=tuple(added),
        removed=tuple(remaining),
        rewritten=tuple(rewritten),
        unchanged=len(kept),
        reordered=kept_in_old != kept,
    )


@dataclass(frozen=True, slots=True)
class DroppedPosition:
    index: int  # 1-based position in the checked entries
    entry: ProfileEntry
    failed: tuple[int, ...]


def dropped_positions(
    entries: Sequence[ProfileEntry], check: EntryCheck
) -> tuple[DroppedPosition, ...]:
    """Locate each dropped entry; ``check.dropped`` keeps the entries' order."""
    remaining = list(check.dropped)
    positions: list[DroppedPosition] = []
    for index, entry in enumerate(entries, start=1):
        if remaining and remaining[0].entry == entry:
            item = remaining.pop(0)
            positions.append(DroppedPosition(index, entry, item.fact_ids))
    return tuple(positions)


@dataclass(frozen=True, slots=True)
class ProfileCheck:
    profile: Profile
    check: EntryCheck


async def check_profiles(store: ProfileStore) -> tuple[ProfileCheck, ...]:
    """§7.4 on the current entries of every stored profile; reads only."""
    return tuple(
        [
            ProfileCheck(
                profile, await store.check(profile.person_id, profile.audience, profile.entries)
            )
            for profile in await store.profiles()
            if profile.entries
        ]
    )


@dataclass(frozen=True, slots=True)
class RenderedConversation:
    conversation_id: str
    kind: str
    owner_id: str  # private: the counterpart's root; space: the space ID
    profiles: tuple[RenderedProfile, ...]
    violations: tuple[str, ...]  # empty when the §7.2/§7.3 placement holds


async def render_check(database: Database) -> tuple[RenderedConversation, ...]:
    """Render every conversation read-only and check where profiles appear: a private
    chat shows only the counterpart's profiles; a group shows only public profiles of
    people on its stored list, at most five entries each. Conversations without a
    profile are left out."""
    resident = ResidentMemory(database)
    async with database.read_session() as session:
        rows = (
            await session.execute(
                select(
                    CanonicalConversationModel.id,
                    CanonicalConversationModel.kind,
                    CanonicalConversationModel.person_id,
                    CanonicalConversationModel.space_id,
                ).order_by(CanonicalConversationModel.id)
            )
        ).all()
        owners = {
            row.id: (
                await person_root(session, row.person_id)
                if row.kind == "private"
                else str(row.space_id)
            )
            for row in rows
        }
    lists = {item.space_id: item.person_ids for item in await ProfileStore(database).all_members()}
    rendered: list[RenderedConversation] = []
    for row in rows:
        profiles = (await resident.render_conversation(row.id)).profiles
        if not profiles:
            continue
        owner = owners[row.id]
        violations: list[str] = []
        for item in profiles:
            label = f"{item.person_id}/{item.audience}"
            if row.kind == "private":
                if item.person_id != owner:
                    violations.append(f"other_person:{label}")
                continue
            if item.audience != "public":
                violations.append(f"private_in_group:{label}")
            if len(item.entries) > MAX_GROUP_MEMBER_ENTRIES:
                violations.append(f"too_many_entries:{label}")
            async with database.read_session() as session:
                listed = {await person_root(session, person) for person in lists.get(owner, ())}
            if item.person_id not in listed:
                violations.append(f"not_listed:{label}")
        rendered.append(RenderedConversation(row.id, row.kind, owner, profiles, tuple(violations)))
    return tuple(rendered)


# ---------------------------------------------------------------- status


@dataclass(frozen=True, slots=True)
class ProfileState:
    profile: Profile
    source: str | None  # source of the current version
    signature_current: bool  # considered signature equals the available-fact signature


@dataclass(frozen=True, slots=True)
class ProfileStatus:
    window: ProfileWindow
    due: tuple[tuple[str, ProfileAudience], ...]
    profiles: tuple[ProfileState, ...]


async def profile_status(database: Database) -> ProfileStatus:
    """Maintenance set of the latest window, what is due (§8.2) and every profile."""
    store = ProfileStore(database)
    window = await latest_window(database)
    profiles = await store.profiles()
    due = [
        (item.person_id, item.audience)
        for item in await due_profiles(
            database, {"public": frozenset(window.public), "private": frozenset(window.private)}
        )
    ]
    states: list[ProfileState] = []
    for profile in profiles:
        version = (
            await store.load_version(profile.person_id, profile.audience, profile.current_version)
            if profile.current_version
            else None
        )
        available = await store.available(profile.person_id, profile.audience)
        states.append(
            ProfileState(
                profile,
                version.source if version is not None else None,
                profile.considered_signature == available.signature,
            )
        )
    return ProfileStatus(window, tuple(due), tuple(states))
