"""Pure identity classifier for C7 backfill.

No argparse, renderer, ORM, or I/O. Callers supply a snapshot; this module
returns a fail-closed plan. New UUIDs are allocated only when the caller
provides a factory, so dry-run can count creates without touching the DB.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

from qq_ai_bot.identity.backfill_types import (
    AccountDecision,
    AccountEvidence,
    BackfillPlan,
    CanonicalPersonRow,
    CanonicalSpaceRow,
    IdentityBindingRow,
    IdentityConflict,
    PresenceRow,
    ShadowAssignment,
    SpaceBindingRow,
    SpaceDecision,
    SpaceEvidence,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.sanitize import fingerprint_external_id

UuidFactory = Callable[[], str]


def _new_uuid4() -> str:
    return str(uuid4())


def classify_account(evidence: AccountEvidence) -> tuple[str, str | None]:
    """Return (class_or_conflict, error_category)."""

    has_binding = evidence.existing_binding_person_id is not None
    has_presence = evidence.existing_presence_id is not None
    if has_binding and has_presence:
        return "conflict", "canonical_kind_mismatch"

    is_yuki = evidence.yuki_self or (has_presence and not has_binding)
    is_person_preconfig = has_binding and not has_presence
    is_external_bot = evidence.ignored_bot or (evidence.legacy_is_bot and not is_yuki)
    has_strong_person = (
        evidence.superuser or evidence.people_human or evidence.strong_person or is_person_preconfig
    )
    has_weak_person = (
        evidence.human_sender
        or evidence.private_peer
        or evidence.member
        or evidence.supporting_person
    )
    if is_yuki and has_strong_person:
        return "conflict", "yuki_and_person"
    if is_yuki and is_external_bot:
        return "conflict", "yuki_and_external_bot"
    if is_external_bot and has_strong_person:
        return "conflict", "person_and_external_bot"
    if is_yuki:
        return "yuki_presence", None
    if is_external_bot:
        return "external_bot", None
    if has_strong_person or has_weak_person:
        return "person", None
    return "conflict", "unclassified"


def _conflict(
    *,
    subject_kind: str,
    kind: str,
    category: str,
    external_id: str,
) -> IdentityConflict:
    return IdentityConflict(
        subject_kind=subject_kind,  # type: ignore[arg-type]
        conflict_kind=kind,  # type: ignore[arg-type]
        error_category=category,  # type: ignore[arg-type]
        platform=IDENTITY_PLATFORM,
        external_id=external_id,
        fingerprint=fingerprint_external_id(external_id),
    )


def _person_populated(person: CanonicalPersonRow | None) -> bool:
    return person is not None


def _space_populated(space: CanonicalSpaceRow | None) -> bool:
    if space is None:
        return False
    return bool(space.name) or space.revision > 1


def _choose_owner[T](
    candidates: set[str],
    rows: Mapping[str, T],
    populated: Callable[[T | None], bool],
) -> tuple[str | None, str | None]:
    if len(candidates) > 1:
        items = tuple(candidates)
        if populated(rows.get(items[0])) and populated(rows.get(items[1])):
            return None, "populated_merge_forbidden"
        return None, "canonical_owner_mismatch"
    if len(candidates) == 1:
        owner_id = next(iter(candidates))
        if owner_id not in rows:
            return None, "canonical_owner_mismatch"
        return owner_id, None
    return None, None


def resolve_account_owner(
    evidence: AccountEvidence,
    classification: str,
    persons: Mapping[str, CanonicalPersonRow],
    bindings: Mapping[str, IdentityBindingRow],
    presences: Mapping[str, PresenceRow],
) -> tuple[str | None, str | None]:
    """Return (reuse_id_or_none, conflict_category_or_none) for the owner object."""

    if classification == "person":
        if evidence.existing_presence_id is not None or evidence.shadow_presence_ids:
            return None, "canonical_kind_mismatch"
        candidates = {
            item
            for item in (
                evidence.existing_person_id,
                evidence.existing_binding_person_id,
                *evidence.shadow_person_ids,
            )
            if item
        }
        owner_id, conflict = _choose_owner(candidates, persons, _person_populated)
        if conflict is not None:
            return None, conflict
        if owner_id is not None:
            binding = bindings.get(evidence.external_id)
            if binding is not None and binding.person_id != owner_id:
                return None, "canonical_owner_mismatch"
        return owner_id, None
    if classification == "yuki_presence":
        if (
            evidence.existing_binding_person_id is not None
            or evidence.existing_person_id is not None
            or evidence.shadow_person_ids
        ):
            return None, "canonical_kind_mismatch"
        candidates = {
            item for item in (evidence.existing_presence_id, *evidence.shadow_presence_ids) if item
        }
        presence = presences.get(evidence.external_id)
        if presence is not None:
            candidates.add(presence.id)
        if len(candidates) > 1:
            return None, "canonical_owner_mismatch"
        if len(candidates) == 1:
            return next(iter(candidates)), None
        return None, None
    if classification == "external_bot":
        if (
            evidence.existing_binding_person_id is not None
            or evidence.existing_person_id is not None
            or evidence.existing_presence_id is not None
            or evidence.shadow_person_ids
            or evidence.shadow_presence_ids
        ):
            return None, "canonical_kind_mismatch"
        return None, None
    return None, "unclassified"


def resolve_space_owner(
    evidence: SpaceEvidence,
    spaces: Mapping[str, CanonicalSpaceRow],
    bindings: Mapping[str, SpaceBindingRow],
) -> tuple[str | None, str | None]:
    candidates = {
        item
        for item in (
            evidence.existing_space_id,
            evidence.existing_binding_space_id,
            *evidence.shadow_space_ids,
        )
        if item
    }
    owner_id, conflict = _choose_owner(candidates, spaces, _space_populated)
    if conflict is not None:
        return None, conflict
    if owner_id is not None:
        binding = bindings.get(evidence.external_id)
        if binding is not None and binding.space_id != owner_id:
            return None, "canonical_owner_mismatch"
    return owner_id, None


def build_plan(
    *,
    accounts: Mapping[str, AccountEvidence],
    spaces: Mapping[str, SpaceEvidence],
    persons: Mapping[str, CanonicalPersonRow],
    identity_bindings: Mapping[str, IdentityBindingRow],
    canonical_spaces: Mapping[str, CanonicalSpaceRow],
    space_bindings: Mapping[str, SpaceBindingRow],
    presences: Mapping[str, PresenceRow],
    shadows: tuple[ShadowAssignment, ...],
    source_fingerprint: str,
    uuid_factory: UuidFactory | None = None,
) -> BackfillPlan:
    """Classify every subject and fail closed on the first contradictory set."""

    new_id = uuid_factory or _new_uuid4
    conflicts: list[IdentityConflict] = []
    decisions: list[AccountDecision] = []
    space_decisions: list[SpaceDecision] = []
    skipped_bots = 0
    processed_subjects = len(accounts) + len(spaces)

    for external_id, evidence in sorted(accounts.items()):
        classification, category = classify_account(evidence)
        if classification == "conflict":
            conflicts.append(
                _conflict(
                    subject_kind="account",
                    kind="unclassified" if category == "unclassified" else "ambiguous_identity",
                    category=category or "unclassified",
                    external_id=external_id,
                )
            )
            continue
        owner_id, owner_conflict = resolve_account_owner(
            evidence, classification, persons, identity_bindings, presences
        )
        if owner_conflict is not None:
            conflicts.append(
                _conflict(
                    subject_kind="account",
                    kind="ambiguous_identity",
                    category=owner_conflict,
                    external_id=external_id,
                )
            )
            continue
        if classification == "external_bot":
            skipped_bots += 1
            decisions.append(
                AccountDecision(
                    external_id=external_id,
                    classification="external_bot",
                    sources=evidence.sources,
                    person_id=None,
                    binding_id=None,
                    presence_id=None,
                    display_name="",
                    create_person=False,
                    create_binding=False,
                    create_presence=False,
                )
            )
            continue
        if classification == "yuki_presence":
            presence_id = owner_id or new_id()
            decisions.append(
                AccountDecision(
                    external_id=external_id,
                    classification="yuki_presence",
                    sources=evidence.sources,
                    person_id=None,
                    binding_id=None,
                    presence_id=presence_id,
                    display_name="",
                    create_person=False,
                    create_binding=False,
                    create_presence=owner_id is None,
                )
            )
            continue
        person_id = owner_id or new_id()
        binding = identity_bindings.get(external_id)
        binding_id = binding.id if binding is not None else new_id()
        decisions.append(
            AccountDecision(
                external_id=external_id,
                classification="person",
                sources=evidence.sources,
                person_id=person_id,
                binding_id=binding_id,
                presence_id=None,
                display_name=evidence.nickname[:128],
                create_person=owner_id is None,
                create_binding=binding is None,
                create_presence=False,
            )
        )

    for external_id, space_evidence in sorted(spaces.items()):
        owner_id, owner_conflict = resolve_space_owner(
            space_evidence, canonical_spaces, space_bindings
        )
        if owner_conflict is not None:
            conflicts.append(
                _conflict(
                    subject_kind="space",
                    kind="ambiguous_identity",
                    category=owner_conflict,
                    external_id=external_id,
                )
            )
            continue
        space_id = owner_id or new_id()
        space_binding = space_bindings.get(external_id)
        binding_id = space_binding.id if space_binding is not None else new_id()
        space_decisions.append(
            SpaceDecision(
                external_id=external_id,
                classification="space",
                sources=space_evidence.sources,
                space_id=space_id,
                binding_id=binding_id,
                name=space_evidence.name[:128],
                enabled=space_evidence.enabled,
                autonomous_enabled=space_evidence.autonomous_enabled,
                require_mention=space_evidence.require_mention,
                display_name=space_evidence.name[:128],
                create_space=owner_id is None,
                create_binding=space_binding is None,
            )
        )

    person_by_account = {
        item.external_id: item.person_id
        for item in decisions
        if item.classification == "person" and item.person_id is not None
    }
    presence_by_account = {
        item.external_id: item.presence_id
        for item in decisions
        if item.classification == "yuki_presence" and item.presence_id is not None
    }
    space_by_external = {item.external_id: item.space_id for item in space_decisions}

    checked_shadows: list[ShadowAssignment] = []
    for shadow in shadows:
        planned = _planned_shadow_value(
            shadow, person_by_account, space_by_external, presence_by_account
        )
        if not shadow.fillable:
            if shadow.current is not None:
                conflicts.append(
                    _conflict(
                        subject_kind=_shadow_subject_kind(shadow),
                        kind="ambiguous_identity",
                        category="canonical_owner_mismatch",
                        external_id=shadow.value,
                    )
                )
            continue
        if planned is None:
            if shadow.current is not None:
                conflicts.append(
                    _conflict(
                        subject_kind=_shadow_subject_kind(shadow),
                        kind="ambiguous_identity",
                        category="canonical_kind_mismatch",
                        external_id=shadow.value,
                    )
                )
            continue
        if shadow.current is not None and shadow.current != planned:
            conflicts.append(
                _conflict(
                    subject_kind=_shadow_subject_kind(shadow),
                    kind="ambiguous_identity",
                    category="canonical_owner_mismatch",
                    external_id=shadow.value,
                )
            )
            continue
        if shadow.current == planned:
            continue
        checked_shadows.append(
            ShadowAssignment(
                table=shadow.table,
                row_key=shadow.row_key,
                column=shadow.column,
                value=planned,
                current=shadow.current,
                fillable=shadow.fillable,
            )
        )

    return BackfillPlan(
        accounts=tuple(decisions),
        spaces=tuple(space_decisions),
        conflicts=tuple(conflicts),
        shadows=tuple(checked_shadows),
        skipped_external_bots=skipped_bots,
        source_fingerprint=source_fingerprint,
        processed_subjects=processed_subjects,
    )


def _planned_shadow_value(
    shadow: ShadowAssignment,
    persons: Mapping[str, str],
    spaces: Mapping[str, str],
    presences: Mapping[str, str],
) -> str | None:
    ref = str(shadow.value)
    if shadow.column.endswith("presence_id") or shadow.column == "canonical_presence_id":
        return presences.get(ref)
    if "space" in shadow.column:
        return spaces.get(ref)
    return persons.get(ref)


def _shadow_subject_kind(shadow: ShadowAssignment) -> str:
    if "space" in shadow.column:
        return "space"
    return "account"
