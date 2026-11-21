"""Resident memory: a small deterministic view of active facts in every Main turn.

See docs/architecture/Yuki-记忆分层P1实施任务书-2026-11-21.md §5. P1 scope per the
owner's decisions (architecture book §8.0): private chats render the counterpart's
explicit facts, the counterpart's core set and SELF principles; group chats render
the group core set and SELF principles only (no speaker / mention sections, Q16 b).
Private-origin facts never render outside the private chat they came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ResidentAudience:
    """Who the resident segment is rendered for, derived from the conversation row."""

    kind: Literal["private", "space"]
    person_id: str | None  # private: counterpart person root ID
    space_id: str | None  # space: the group's space ID


@dataclass(frozen=True, slots=True)
class ResidentRender:
    text: str  # empty: this turn has no resident segment
    fact_ids: tuple[int, ...]  # rendered fact IDs in order of appearance
    omitted_explicit: int  # explicit facts not rendered because of the cap
