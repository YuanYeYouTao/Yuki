"""Trusted read identity for externalized model research material."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtifactAccess:
    conversation_id: str
    generation: int
    actor_person_id: str
    principal_kind: str = "person"
    read_scope: str = ""

    def __post_init__(self) -> None:
        if (
            not self.conversation_id
            or self.generation < 1
            or self.principal_kind not in {"person", "self"}
            or (self.principal_kind == "person" and not self.actor_person_id)
        ):
            raise ValueError("artifact_source_incomplete")

    def encode(self, privacy_generation: int) -> str:
        return json.dumps(
            {**asdict(self), "privacy_generation": privacy_generation}, sort_keys=True
        )


def access_from_source(
    conversation_id: str, generation: int, source: dict[str, Any]
) -> ArtifactAccess:
    """Only the host's already authenticated source may populate this value."""
    return ArtifactAccess(
        conversation_id,
        generation,
        str(source.get("actor_person_id") or ""),
        str(source.get("principal_kind") or "person"),
        str(source.get("read_scope") or ""),
    )


def access_from_runtime(runtime: Any, *, generation: int | None = None) -> ArtifactAccess:
    actor = runtime.require_actor()
    snapshot = runtime.turn_snapshot
    if snapshot is not None:
        generation = snapshot.generation
    if generation is None:
        raise ValueError("artifact_source_incomplete")
    # This is a description of the authenticated reading contract, never a grant.
    scope = json.dumps(
        {
            "memory": sorted(str(value) for value in (runtime.memory_allowed_scopes or ())),
            "plugin_id": runtime.context_plugin_id,
            "delegation_id": runtime.context_read_contract,
        },
        sort_keys=True,
    )
    return ArtifactAccess(
        str(runtime.effective_conversation_id or ""),
        generation,
        str(actor.person_id or ""),
        actor.principal_kind,
        scope,
    )
