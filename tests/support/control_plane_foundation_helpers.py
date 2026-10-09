"""Application assembly and usable control read/write contracts."""

from __future__ import annotations

from datetime import UTC, datetime

from qq_ai_bot.control_plane import (
    ControlPrincipal,
    DecisionContext,
    PrincipalSource,
)
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId

_NOW = datetime(2026, 9, 27, tzinfo=UTC)


def context(*capabilities: str) -> DecisionContext:
    principal = ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=PersonId.new(),
        source=PrincipalSource.CLI,
        roles=("maintainer",),
        granted_capabilities=capabilities,
        authenticated=True,
        active=True,
    )
    return DecisionContext(
        request_id=RequestId.new(),
        principal=principal,
        source=principal.source,
        canonical_target=principal.person_id,
        reason="control-foundation-test",
    )
