"""Person profiles (layered memory P4, #227): storage, versions, member lists, eligibility.

Contract: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md §4-§7. Each person
root has at most one public and one private profile. The current entries live only
in the version row the profile points at; versions are appended and never
rewritten. Forgetting a person deletes whole profiles of the affected persons
(``PeopleRepository._forget_person_memory``); member rows cascade with their person.

Every person ID is resolved to its root in a reader before any write. Writes are
short transactions that compare only the version and lock read in the same
transaction; no model, network or history scan runs inside them. Eligibility (§6)
and the per-entry check (§7.4) only read; rendering, the Dream selection step and
the owner CLI share them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.identity.person_identity import person_family_condition, person_root_id
from yuki.memory.resident import (
    evidence_origins,
    load_facts,
    private_profile_evidence,
    public_profile_evidence,
    resolve_listed,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    MemoryFactModel,
    MemoryProfileModel,
    MemoryProfileVersionModel,
    MemorySpaceMemberModel,
)

MAX_PROFILE_ENTRIES = 10  # entries per profile
MAX_PROFILE_ENTRY_CHARS = 40  # characters per entry after folding whitespace
MAX_PROFILE_RENDERED_IDS = 2  # fact IDs shown per rendered entry; every ID is checked
MAX_SPACE_MEMBERS = 5  # displayed members per group
MAX_GROUP_MEMBER_ENTRIES = 5  # entries per member in a group (P4-Q1); private chats show all
PROFILE_SECTIONS = ("身份", "关系", "兴趣", "做事方式", "相处")

ProfileAudience = Literal["public", "private"]
ProfileSource = Literal["dream", "owner", "program"]
MembersSource = Literal["dream", "owner"]


class ProfileValidationError(ValueError):
    """A write that does not satisfy the profile contract; ``code`` names the rule."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ProfileConflict(RuntimeError):
    """The profile changed after the caller read it; nothing was written.

    ``version_changed``: ``current_version`` differs from the expected version.
    ``locked``: the caller required an unlocked profile and the owner locked it.
    """

    def __init__(self, code: Literal["version_changed", "locked"]) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProfileEntry:
    section: str | None  # one of PROFILE_SECTIONS, or None
    text: str
    fact_ids: tuple[int, ...]  # every supporting fact, in the entry's own order

    def to_json(self) -> dict[str, Any]:
        return {"section": self.section, "text": self.text, "fact_ids": list(self.fact_ids)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ProfileEntry:
        return cls(
            section=value["section"],
            text=str(value["text"]),
            fact_ids=tuple(int(item) for item in value["fact_ids"]),
        )


@dataclass(frozen=True, slots=True)
class ProfileVersion:
    version: int
    entries: tuple[ProfileEntry, ...]
    source: ProfileSource
    reason: str
    dream_run_public_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Profile:
    person_id: str  # the person root the profile is stored under
    audience: ProfileAudience
    current_version: int  # 0: no version yet
    entries: tuple[ProfileEntry, ...]  # the current version's entries
    considered_signature: str | None
    attempted_at: datetime | None
    locked: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProfileCommit:
    profile: Profile
    written: bool  # False: the entries equalled the current ones, no version was added


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    """The facts a profile may cite now (§6), ascending, and their signature."""

    fact_ids: tuple[int, ...]
    signature: str


@dataclass(frozen=True, slots=True)
class DroppedEntry:
    entry: ProfileEntry
    fact_ids: tuple[int, ...]  # the IDs without an available active end of chain


@dataclass(frozen=True, slots=True)
class EntryCheck:
    """§7.4 result: an entry passes only if every ID resolves to an available fact."""

    entries: tuple[ProfileEntry, ...]  # passing entries, IDs mapped to active successors
    dropped: tuple[DroppedEntry, ...]
    replaced: tuple[tuple[int, int], ...]  # (listed ID, successor) of passing entries

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.replaced)


@dataclass(frozen=True, slots=True)
class SpaceMembers:
    space_id: str
    person_ids: tuple[str, ...]  # person roots in display order
    source: MembersSource
    dream_run_public_id: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RenderedProfile:
    """One profile as a turn renders it: checked entries with successor IDs."""

    person_id: str
    audience: ProfileAudience
    entries: tuple[ProfileEntry, ...]


PROFILE_TITLES: dict[ProfileAudience, str] = {
    "public": "[对方·公共画像]",
    "private": "[对方·私聊画像]",
}
GROUP_PROFILES_TITLE = "[群成员画像]"


def profile_signature(fact_ids: Iterable[int]) -> str:
    """SHA-256 of the ascending fact IDs joined by commas."""
    return hashlib.sha256(",".join(str(item) for item in sorted(fact_ids)).encode()).hexdigest()


def validate_entries(entries: Sequence[ProfileEntry]) -> tuple[ProfileEntry, ...]:
    """Structural rules (§7.1, §8.4); returns the entries with whitespace folded."""
    if len(entries) > MAX_PROFILE_ENTRIES:
        raise ProfileValidationError("too_many_entries", str(len(entries)))
    checked: list[ProfileEntry] = []
    for entry in entries:
        if "\n" in entry.text or "\r" in entry.text:
            raise ProfileValidationError("multiline_text")
        text = " ".join(entry.text.split())
        if not text:
            raise ProfileValidationError("empty_text")
        if len(text) > MAX_PROFILE_ENTRY_CHARS:
            raise ProfileValidationError("text_too_long", text)
        if entry.section is not None and entry.section not in PROFILE_SECTIONS:
            raise ProfileValidationError("unknown_section", entry.section)
        if not entry.fact_ids:
            raise ProfileValidationError("empty_facts", text)
        if len(set(entry.fact_ids)) != len(entry.fact_ids):
            raise ProfileValidationError("duplicate_facts", text)
        checked.append(ProfileEntry(entry.section, text, tuple(entry.fact_ids)))
    return tuple(checked)


async def person_root(session: AsyncSession, person_id: str) -> str:
    root = await session.scalar(select(person_root_id(person_id)))
    if root is None:
        raise ProfileValidationError("unknown_person", person_id)
    return str(root)


async def profile_fact_ids(
    session: AsyncSession, root: str, audience: ProfileAudience
) -> tuple[int, ...]:
    """Facts ``root``'s profile may cite (§6), ascending.

    Person scope only; the common resident conditions; owned by the root (merged
    persons included). Public: ``public_profile_evidence``. Private: not an explicit
    fact (those render on their own) and ``private_profile_evidence``.
    """
    fact = MemoryFactModel
    facts = await load_facts(
        session,
        fact.scope_type == "person",
        person_family_condition(fact.canonical_subject_person_id, root),
    )
    candidates = [
        item
        for item in facts.values()
        if item.eligible
        and item.subject_root == root
        and (audience == "public" or not item.explicit)
    ]
    origins = await evidence_origins(session, (item.id for item in candidates))
    rule = public_profile_evidence if audience == "public" else private_profile_evidence
    return tuple(sorted(item.id for item in candidates if rule(origins.get(item.id, ()), root)))


async def check_entries(
    session: AsyncSession,
    root: str,
    audience: ProfileAudience,
    entries: Sequence[ProfileEntry],
) -> EntryCheck:
    """§7.4: map every ID to its active successor and require it to be available."""
    available = set(await profile_fact_ids(session, root, audience))
    listed = tuple(dict.fromkeys(item for entry in entries for item in entry.fact_ids))
    _, current = await resolve_listed(session, listed)
    kept: list[ProfileEntry] = []
    dropped: list[DroppedEntry] = []
    replaced: dict[int, int] = {}
    for entry in entries:
        failed = tuple(item for item in entry.fact_ids if current.get(item) not in available)
        if failed:
            dropped.append(DroppedEntry(entry, failed))
            continue
        replaced.update((item, current[item]) for item in entry.fact_ids if current[item] != item)
        mapped = tuple(dict.fromkeys(current[item] for item in entry.fact_ids))
        kept.append(replace(entry, fact_ids=mapped))
    return EntryCheck(tuple(kept), tuple(dropped), tuple(sorted(replaced.items())))


def entry_line(entry: ProfileEntry) -> str:
    """``- [小节] 正文（fact:a,b）``: every ID was checked, the first two are shown."""
    section = f"[{entry.section}] " if entry.section else ""
    shown = ",".join(str(item) for item in entry.fact_ids[:MAX_PROFILE_RENDERED_IDS])
    return f"- {section}{entry.text}（fact:{shown}）"


async def _rendered(
    session: AsyncSession, root: str, audience: ProfileAudience
) -> tuple[ProfileEntry, ...]:
    row = await session.scalar(_profile_statement(root, audience))
    if row is None or row.current_version == 0:
        return ()
    entries = await _entries(session, row.id, row.current_version)
    return (await check_entries(session, root, audience, entries)).entries if entries else ()


async def render_private_profiles(
    session: AsyncSession, root: str
) -> tuple[list[str], tuple[RenderedProfile, ...]]:
    """§7.2: the counterpart's public and private profiles, every passing entry."""
    lines: list[str] = []
    rendered: list[RenderedProfile] = []
    audiences: tuple[ProfileAudience, ...] = ("public", "private")
    for audience in audiences:
        entries = await _rendered(session, root, audience)
        if entries:
            lines.append(PROFILE_TITLES[audience])
            lines.extend(entry_line(entry) for entry in entries)
            rendered.append(RenderedProfile(root, audience, entries))
    return lines, tuple(rendered)


async def render_space_profiles(
    session: AsyncSession, space_id: str
) -> tuple[list[str], tuple[RenderedProfile, ...]]:
    """§7.3: the stored member list's public profiles, first entries of each.

    Members resolve to their root (a member merged after the list was written shows
    once, under the root); the name line lists the root ID and every person ID merged
    into it, so history envelopes with an old ID match. Members without a passing
    entry are left out.
    """
    roots: list[str] = []
    for person_id in await session.scalars(
        select(MemorySpaceMemberModel.person_id)
        .where(MemorySpaceMemberModel.space_id == space_id)
        .order_by(MemorySpaceMemberModel.position)
    ):
        root = await person_root(session, person_id)
        if root not in roots:
            roots.append(root)
    lines: list[str] = []
    rendered: list[RenderedProfile] = []
    for root in roots:
        entries = (await _rendered(session, root, "public"))[:MAX_GROUP_MEMBER_ENTRIES]
        if not entries:
            continue
        name = await session.scalar(
            select(CanonicalPersonModel.display_name).where(CanonicalPersonModel.id == root)
        )
        merged = tuple(
            await session.scalars(
                select(CanonicalPersonModel.id)
                .where(CanonicalPersonModel.merged_into_person_id == root)
                .order_by(CanonicalPersonModel.id)
            )
        )
        also = "；同一人另有 " + "、".join(f"person:{item}" for item in merged) if merged else ""
        lines.append(f"· {' '.join(str(name or '').split())}（person:{root}{also}）")
        lines.extend(entry_line(entry) for entry in entries)
        rendered.append(RenderedProfile(root, "public", entries))
    if lines:
        lines.insert(0, GROUP_PROFILES_TITLE)
    return lines, tuple(rendered)


class ProfileStore:
    """Profiles, their versions and the group member lists."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ reads
    async def load(self, person_id: str, audience: ProfileAudience) -> Profile | None:
        """The root's profile with its current entries; None when it has no row."""
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
            row = await session.scalar(_profile_statement(root, audience))
            if row is None:
                return None
            return _profile(row, await _entries(session, row.id, row.current_version))

    async def load_version(
        self, person_id: str, audience: ProfileAudience, version: int
    ) -> ProfileVersion | None:
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
            row = await session.scalar(
                select(MemoryProfileVersionModel)
                .join(
                    MemoryProfileModel,
                    MemoryProfileModel.id == MemoryProfileVersionModel.profile_id,
                )
                .where(
                    MemoryProfileModel.person_id == root,
                    MemoryProfileModel.audience == audience,
                    MemoryProfileVersionModel.version == version,
                )
            )
        return None if row is None else _version(row)

    async def history(
        self, person_id: str, audience: ProfileAudience
    ) -> tuple[ProfileVersion, ...]:
        """Every version, oldest first."""
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
            rows = await session.scalars(
                select(MemoryProfileVersionModel)
                .join(
                    MemoryProfileModel,
                    MemoryProfileModel.id == MemoryProfileVersionModel.profile_id,
                )
                .where(
                    MemoryProfileModel.person_id == root, MemoryProfileModel.audience == audience
                )
                .order_by(MemoryProfileVersionModel.version)
            )
            return tuple(_version(row) for row in rows)

    async def profiles(self) -> tuple[Profile, ...]:
        """Every stored profile with its current entries, by person and audience."""
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    select(MemoryProfileModel, MemoryProfileVersionModel.entries_json)
                    .outerjoin(
                        MemoryProfileVersionModel,
                        (MemoryProfileVersionModel.profile_id == MemoryProfileModel.id)
                        & (MemoryProfileVersionModel.version == MemoryProfileModel.current_version),
                    )
                    .order_by(MemoryProfileModel.person_id, MemoryProfileModel.audience)
                )
            ).all()
        return tuple(_profile(row, _decode(entries)) for row, entries in rows)

    async def available(self, person_id: str, audience: ProfileAudience) -> ProfileFacts:
        """The root's currently available facts (§6) and their signature (§8.2)."""
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
            fact_ids = await profile_fact_ids(session, root, audience)
        return ProfileFacts(fact_ids, profile_signature(fact_ids))

    async def check(
        self, person_id: str, audience: ProfileAudience, entries: Sequence[ProfileEntry]
    ) -> EntryCheck:
        """§7.4 for entries the caller is about to write or render; reads only."""
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
            return await check_entries(session, root, audience, entries)

    # ----------------------------------------------------------------- writes
    async def commit(
        self,
        person_id: str,
        audience: ProfileAudience,
        entries: Sequence[ProfileEntry],
        *,
        source: ProfileSource,
        reason: str,
        expected_version: int,
        signature: str | None = None,
        attempted_at: datetime | None = None,
        lock: bool | None = None,
        require_unlocked: bool = False,
        dream_run_public_id: str | None = None,
    ) -> ProfileCommit:
        """Write ``entries`` as the next version if they differ from the current ones.

        ``expected_version`` is the version the caller based the entries on (0 for a
        profile without versions); any other current version raises
        ``ProfileConflict("version_changed")``. ``require_unlocked`` (the Dream step)
        raises ``ProfileConflict("locked")`` on a locked profile. Identical entries add
        no version; ``signature``, ``attempted_at`` and ``lock`` are stored either way
        when given. Entries are validated structurally here; whether their facts are
        available is the caller's ``check``.
        """
        checked = validate_entries(entries)
        if not reason.strip():
            raise ProfileValidationError("empty_reason")
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.scalar(_profile_statement(root, audience))
            if (row.current_version if row is not None else 0) != expected_version:
                raise ProfileConflict("version_changed")
            if require_unlocked and row is not None and row.locked:
                raise ProfileConflict("locked")
            if row is None:
                row = await _new_row(session, root, audience, now)
            current = await _entries(session, row.id, row.current_version)
            written = checked != current
            if written:
                row.current_version += 1
                session.add(
                    MemoryProfileVersionModel(
                        profile_id=row.id,
                        version=row.current_version,
                        entries_json=_encode(checked),
                        source=source,
                        reason=reason.strip(),
                        dream_run_public_id=dream_run_public_id,
                        created_at=now,
                    )
                )
            if signature is not None:
                row.considered_signature = signature
            if attempted_at is not None:
                row.attempted_at = attempted_at
            if lock is not None:
                row.locked = lock
            row.updated_at = now
            await session.flush()
            return ProfileCommit(_profile(row, checked if written else current), written)

    async def record_attempt(
        self, person_id: str, audience: ProfileAudience, attempted_at: datetime
    ) -> None:
        """Store the attempt time of a voided call; entries and signature stay."""
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.scalar(_profile_statement(root, audience))
            if row is None:
                row = await _new_row(session, root, audience, now)
            row.attempted_at = attempted_at
            row.updated_at = now

    async def set_locked(self, person_id: str, audience: ProfileAudience, locked: bool) -> Profile:
        """Lock (the Dream step skips the model call) or unlock; cleanup still runs."""
        async with self._database.read_session() as session:
            root = await person_root(session, person_id)
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.scalar(_profile_statement(root, audience))
            if row is None:
                row = await _new_row(session, root, audience, now)
            row.locked = locked
            row.updated_at = now
            await session.flush()
            return _profile(row, await _entries(session, row.id, row.current_version))

    # ---------------------------------------------------------------- members
    async def members(self, space_id: str) -> SpaceMembers | None:
        async with self._database.read_session() as session:
            rows = (
                await session.scalars(
                    select(MemorySpaceMemberModel)
                    .where(MemorySpaceMemberModel.space_id == space_id)
                    .order_by(MemorySpaceMemberModel.position)
                )
            ).all()
        return _members(space_id, rows) if rows else None

    async def all_members(self) -> tuple[SpaceMembers, ...]:
        async with self._database.read_session() as session:
            rows = (
                await session.scalars(
                    select(MemorySpaceMemberModel).order_by(
                        MemorySpaceMemberModel.space_id, MemorySpaceMemberModel.position
                    )
                )
            ).all()
        by_space: dict[str, list[MemorySpaceMemberModel]] = {}
        for row in rows:
            by_space.setdefault(row.space_id, []).append(row)
        return tuple(_members(space_id, items) for space_id, items in by_space.items())

    async def pin_members(self, space_id: str, person_ids: Sequence[str]) -> SpaceMembers:
        """Owner list (``source='owner'``): 1-5 distinct roots; Dream leaves it alone."""
        if not person_ids:
            raise ProfileValidationError("no_members")
        if len(person_ids) > MAX_SPACE_MEMBERS:
            raise ProfileValidationError("too_many_members", str(len(person_ids)))
        async with self._database.read_session() as session:
            if await session.get(CanonicalSpaceModel, space_id) is None:
                raise ProfileValidationError("unknown_space", space_id)
            roots = tuple([await person_root(session, item) for item in person_ids])
        if len(set(roots)) != len(roots):
            raise ProfileValidationError("duplicate_member")
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            await _replace_members(session, space_id, roots, "owner", None, now)
        return SpaceMembers(space_id, roots, "owner", None, now)

    async def unpin_members(self, space_id: str) -> SpaceMembers | None:
        """Hand the list back to Dream; the rows stay until the next recomputation."""
        async with self._database.immediate_session() as session:
            await session.execute(
                update(MemorySpaceMemberModel)
                .where(MemorySpaceMemberModel.space_id == space_id)
                .values(source="dream", updated_at=datetime.now(UTC))
            )
        return await self.members(space_id)

    async def replace_dream_members(
        self, ranking: Mapping[str, Sequence[str]], *, dream_run_public_id: str
    ) -> tuple[str, ...]:
        """Replace each given space's Dream list with its ranked roots in one short
        transaction; spaces with an owner list are skipped. Returns the spaces written."""
        now = datetime.now(UTC)
        written: list[str] = []
        async with self._database.immediate_session() as session:
            for space_id, roots in ranking.items():
                pinned = await session.scalar(
                    select(MemorySpaceMemberModel.position).where(
                        MemorySpaceMemberModel.space_id == space_id,
                        MemorySpaceMemberModel.source == "owner",
                    )
                )
                if pinned is not None:
                    continue
                await _replace_members(
                    session, space_id, tuple(roots), "dream", dream_run_public_id, now
                )
                written.append(space_id)
        return tuple(written)


def _profile_statement(root: str, audience: ProfileAudience) -> Any:
    return select(MemoryProfileModel).where(
        MemoryProfileModel.person_id == root, MemoryProfileModel.audience == audience
    )


async def _new_row(
    session: AsyncSession, root: str, audience: ProfileAudience, now: datetime
) -> MemoryProfileModel:
    row = MemoryProfileModel(
        person_id=root,
        audience=audience,
        current_version=0,
        locked=False,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def _entries(
    session: AsyncSession, profile_id: int, version: int
) -> tuple[ProfileEntry, ...]:
    if version == 0:
        return ()
    return _decode(
        await session.scalar(
            select(MemoryProfileVersionModel.entries_json).where(
                MemoryProfileVersionModel.profile_id == profile_id,
                MemoryProfileVersionModel.version == version,
            )
        )
    )


def _encode(entries: Sequence[ProfileEntry]) -> str:
    return json.dumps([entry.to_json() for entry in entries], ensure_ascii=False)


def _decode(value: str | None) -> tuple[ProfileEntry, ...]:
    return (
        () if value is None else tuple(ProfileEntry.from_json(item) for item in json.loads(value))
    )


def _utc(value: datetime) -> datetime:
    """SQLite returns stored UTC values without a zone."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _profile(row: MemoryProfileModel, entries: tuple[ProfileEntry, ...]) -> Profile:
    audience: ProfileAudience = "private" if row.audience == "private" else "public"
    return Profile(
        person_id=row.person_id,
        audience=audience,
        current_version=row.current_version,
        entries=entries,
        considered_signature=row.considered_signature,
        attempted_at=None if row.attempted_at is None else _utc(row.attempted_at),
        locked=row.locked,
        updated_at=_utc(row.updated_at),
    )


def _version(row: MemoryProfileVersionModel) -> ProfileVersion:
    sources: dict[str, ProfileSource] = {"dream": "dream", "owner": "owner", "program": "program"}
    return ProfileVersion(
        version=row.version,
        entries=_decode(row.entries_json),
        source=sources[row.source],
        reason=row.reason,
        dream_run_public_id=row.dream_run_public_id,
        created_at=_utc(row.created_at),
    )


def _members(space_id: str, rows: Sequence[MemorySpaceMemberModel]) -> SpaceMembers:
    source: MembersSource = "owner" if rows[0].source == "owner" else "dream"
    return SpaceMembers(
        space_id=space_id,
        person_ids=tuple(row.person_id for row in rows),
        source=source,
        dream_run_public_id=rows[0].dream_run_public_id,
        updated_at=max(_utc(row.updated_at) for row in rows),
    )


async def _replace_members(
    session: AsyncSession,
    space_id: str,
    roots: tuple[str, ...],
    source: MembersSource,
    dream_run_public_id: str | None,
    now: datetime,
) -> None:
    await session.execute(
        delete(MemorySpaceMemberModel).where(MemorySpaceMemberModel.space_id == space_id)
    )
    session.add_all(
        MemorySpaceMemberModel(
            space_id=space_id,
            position=position,
            person_id=root,
            source=source,
            dream_run_public_id=dream_run_public_id,
            updated_at=now,
        )
        for position, root in enumerate(roots, start=1)
    )
    await session.flush()
