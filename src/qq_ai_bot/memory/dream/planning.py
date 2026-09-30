"""Short-lived Dream plan sources; canonical facts remain the owner/version authority."""

from dataclasses import dataclass
from typing import Any

from qq_ai_bot.identity.ownership import DreamShape, optional_dream_owner_from_facts
from qq_ai_bot.persistence.unit_of_work import state_revision

DreamClusterSpec = tuple[str, str, str, str, tuple[int, ...], str]


@dataclass(frozen=True, slots=True)
class PreparedDreamFact:
    id: int
    revision: int
    scope_type: str
    visibility_type: str | None
    kind: str
    status: str
    review_state: str
    canonical_subject_person_id: str | None
    canonical_subject_space_id: str | None
    canonical_visibility_person_id: str | None
    canonical_visibility_space_id: str | None

    @classmethod
    def from_fact(cls, fact: Any) -> "PreparedDreamFact":
        def value(name: str) -> Any:
            item = getattr(fact, name)
            return getattr(item, "value", item)

        return cls(
            id=fact.id,
            revision=state_revision(fact.updated_at),
            scope_type=value("scope_type"),
            visibility_type=value("visibility_type"),
            kind=value("kind"),
            status=value("status"),
            review_state=value("review_state"),
            canonical_subject_person_id=fact.canonical_subject_person_id,
            canonical_subject_space_id=fact.canonical_subject_space_id,
            canonical_visibility_person_id=fact.canonical_visibility_person_id,
            canonical_visibility_space_id=fact.canonical_visibility_space_id,
        )


@dataclass(frozen=True, slots=True)
class PreparedDreamCluster:
    spec: DreamClusterSpec
    owner: DreamShape
    facts: tuple[PreparedDreamFact, ...]


def prepare_clusters_from_facts(
    clusters: tuple[DreamClusterSpec, ...], facts: tuple[Any, ...]
) -> tuple[PreparedDreamCluster, ...]:
    sources = {fact.id: PreparedDreamFact.from_fact(fact) for fact in facts}
    if len({cluster[0] for cluster in clusters}) != len(clusters):
        raise ValueError("duplicate_dream_cluster")
    prepared = []
    for spec in clusters:
        fact_ids = spec[4]
        if not fact_ids or not set(fact_ids).issubset(sources):
            raise ValueError("incomplete_dream_owner")
        selected = tuple(sources[identity] for identity in fact_ids)
        shape = optional_dream_owner_from_facts(selected)
        if shape is None or any(fact.kind != spec[3] for fact in selected):
            raise ValueError("incomplete_dream_owner")
        prepared.append(PreparedDreamCluster(spec, shape, selected))
    return tuple(prepared)
