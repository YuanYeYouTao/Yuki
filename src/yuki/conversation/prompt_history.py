"""Projection-owned raw metadata and exact unadmitted source membership."""

from __future__ import annotations

import sys
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Collection, Iterable
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from yuki.conversation.frozen_fragments import EventFragment
from yuki.conversation.projections import ProjectionConflict
from yuki.event_prompt import ChatEventPromptRenderer, EventPromptIndex, EventPromptReferences
from yuki.persistence.repository_records import EventRecord
from yuki.time.formatting import local_datetime, stored_utc

if TYPE_CHECKING:
    from yuki.conversation.projections import ProjectionSnapshot


@dataclass(frozen=True, slots=True)
class _RawEvent:
    row: EventRecord
    key: tuple[str, tuple[str, ...], str, str, int | None]
    utc: datetime
    day: date
    visible: bool


_RECORD_FIELDS = fields(EventRecord)


def _metadata_size(item: _RawEvent) -> int:
    # Count per-row retained objects once inside the row. Shared strings across
    # different rows are conservatively counted again, without an O(N) global
    # deduplication set on the append path.
    values = (
        item,
        item.row,
        item.key,
        item.key[1],
        *item.key,
        *item.key[1],
        item.utc,
        item.day,
        *(getattr(item.row, field.name) for field in _RECORD_FIELDS),
    )
    seen: set[int] = set()
    size = 0
    for value in values:
        if id(value) not in seen:
            seen.add(id(value))
            size += sys.getsizeof(value)
    return size


def _continues(first: _RawEvent, last: _RawEvent, current: _RawEvent) -> bool:
    return (
        first.key == current.key
        and last.utc <= current.utc
        and first.utc <= current.utc <= first.utc + timedelta(minutes=5)
        and first.day == current.day
    )


class PromptHistorySource:
    """Recyclable derived data for one exact source/coverage/renderer stamp.

    Raw metadata always includes the current event. Only the candidate view
    excludes it. Pending IDs leave this owner only after a committed head proves
    their frozen representation; scanning or a failed preparation is not admission.
    """

    def __init__(
        self,
        stamp: tuple[object, ...],
        *,
        projection: ProjectionSnapshot | None,
        bot_display_name: str,
        timezone: str,
        after_event_id: int,
    ) -> None:
        self.stamp = stamp
        self.scanned_through = after_event_id
        self._after_event_id = after_event_id
        self.references = EventPromptIndex()
        self._metadata: list[_RawEvent] = []
        self._positions: dict[int, int] = {}
        self._raw_ids: list[int] = []
        self._groups: list[list[int]] = []
        self._group_of: dict[int, int] = {}
        self._visible_ids: list[int] = []
        # Linked iteration follows actual holes after admission, rather than
        # walking a plain dict's old deleted slots from a large cold candidate.
        self._pending: OrderedDict[int, None] = OrderedDict()
        self._metadata_bytes = 0
        self._group_bytes = 0
        self._published = projection.fragments if projection is not None else None
        self._bot_display_name, self._timezone = bot_display_name, timezone

    @property
    def estimated_bytes(self) -> int:
        """Conservative retained-memory estimate, updated only by actual delta rows."""
        return (
            self._metadata_bytes
            + self._group_bytes
            + self.references.estimated_bytes
            + sum(
                sys.getsizeof(value)
                for value in (
                    self,
                    self.__dict__,
                    self.stamp,
                    self._metadata,
                    self._positions,
                    self._raw_ids,
                    self._groups,
                    self._group_of,
                    self._visible_ids,
                    self._pending,
                )
            )
        )

    def raw_tail(self, through_event_id: int) -> int:
        end = bisect_right(self._raw_ids, through_event_id)
        return self._raw_ids[end - 1] if end else self._after_event_id

    def reconcile(self, projection: ProjectionSnapshot | None) -> bool:
        """Apply only newly observed committed chunks, never candidate membership."""
        if projection is None:
            return self._published is None
        if self._published is projection.fragments:
            return True
        if self._published is None:
            blocks = projection.fragments.iter_blocks()
        else:
            try:
                blocks = iter(projection.fragments.delta_from(self._published))
            except ProjectionConflict:
                return False
        for block in blocks:
            for fragment in block:
                for event_id in fragment.event_ids:
                    self._pending.pop(event_id, None)
        self._published = projection.fragments
        return True

    def extend(self, rows: tuple[EventRecord, ...], *, through_event_id: int) -> None:
        renderer = ChatEventPromptRenderer(
            rows, bot_display_name=self._bot_display_name, timezone=self._timezone
        )
        metadata = []
        for row in rows:
            if row.id in self._positions:
                continue
            visible = bool(renderer.event_content(row, None, ""))
            item = _RawEvent(
                replace(
                    row,
                    content="",
                    projected_content=None,
                    projected_mentions=(),
                    segments=(),
                    visual_summary="",
                    audio_transcript="",
                    mentioned_user_ids=(),
                    mentioned_person_ids=(),
                    external_payload=None,
                ),
                renderer.group_identity(row),
                stored_utc(row.occurred_at),
                local_datetime(row.occurred_at, self._timezone).date(),
                visible,
            )
            self._positions[row.id] = len(self._metadata)
            self._raw_ids.append(row.id)
            self._metadata.append(item)
            self._metadata_bytes += _metadata_size(item) + sys.getsizeof(len(self._metadata) - 1)
            metadata.append(item.row)
            if not visible:
                continue
            self._visible_ids.append(row.id)
            if self._published is None or row.id not in self._published.event_ids:
                self._pending[row.id] = None
            if self._groups:
                prior = self._groups[-1]
                if _continues(self._at(prior[0]), self._at(prior[-1]), item):
                    before = sys.getsizeof(prior)
                    prior.append(row.id)
                    self._group_bytes += sys.getsizeof(prior) - before
                else:
                    self._groups.append([row.id])
                    self._group_bytes += sys.getsizeof(self._groups[-1])
            else:
                self._groups.append([row.id])
                self._group_bytes += sys.getsizeof(self._groups[-1])
            self._group_of[row.id] = len(self._groups) - 1
        self.references.extend(metadata)
        self.scanned_through = max(self.scanned_through, through_event_id)

    def _at(self, event_id: int) -> _RawEvent:
        return self._metadata[self._positions[event_id]]

    def pending(self, through_event_id: int) -> tuple[int, ...]:
        """Freeze only holes/new rows, not the entire old admitted ID set."""
        return tuple(event_id for event_id in self._pending if event_id <= through_event_id)

    def candidate_pending(
        self,
        captured: tuple[int, ...],
        additions: tuple[EventRecord, ...],
        *,
        frozen_event_ids: Collection[int],
    ) -> tuple[int, ...]:
        """Use this candidate's head and holes, even if another head committed while reading."""
        # The query starts above the same captured scan boundary, so additions
        # follow captured holes in raw order and cannot duplicate them.
        return (
            *captured,
            *(
                row.id
                for row in additions
                if self._at(row.id).visible and row.id not in frozen_event_ids
            ),
        )

    def capture(
        self, through_event_id: int, *, extra_events: Iterable[EventRecord] = ()
    ) -> EventPromptReferences:
        return self.references.capture(through_event_id=through_event_id, extra_events=extra_events)

    def _excluded_groups(
        self, current_event_id: int | None, through_event_id: int
    ) -> dict[int, tuple[int, ...]]:
        """Recompute only the propagation window until original grouping resumes."""
        if current_event_id not in self._group_of:
            return {}
        assert current_event_id is not None
        group_number = self._group_of[current_event_id]
        visible_end = bisect_right(self._visible_ids, through_event_id)
        if visible_end and current_event_id == self._visible_ids[visible_end - 1]:
            return {}
        first_group = self._groups[max(0, group_number - 1)]
        start = self._positions[first_group[0]]
        groups: list[list[int]] = []
        passed = False
        for position in range(start, len(self._metadata)):
            item = self._metadata[position]
            event_id = item.row.id
            if event_id > through_event_id:
                break
            if event_id == current_event_id:
                passed = True
                continue
            if not item.visible:
                continue
            if groups and _continues(self._at(groups[-1][0]), self._at(groups[-1][-1]), item):
                groups[-1].append(event_id)
            else:
                groups.append([event_id])
            original = self._groups[self._group_of[event_id]]
            if passed and groups[-1][0] == original[0]:
                groups.pop()
                break
        affected = {}
        for group in groups:
            ids = tuple(group)
            for event_id in group:
                affected[event_id] = ids
        return affected

    def render(
        self,
        rows: tuple[EventRecord, ...],
        *,
        references: EventPromptReferences,
        through_event_id: int,
        current_event_id: int | None,
        yuki_account_ids: frozenset[str],
    ) -> tuple[tuple[EventFragment, ...], tuple[EventFragment, ...]]:
        renderer = ChatEventPromptRenderer(
            references=references,
            bot_display_name=self._bot_display_name,
            timezone=self._timezone,
            yuki_account_ids=yuki_account_ids,
        )
        fresh = {row.id: row for row in rows if row.id != current_event_id}
        overrides = self._excluded_groups(current_event_id, through_event_id)
        selected: dict[int, tuple[int, list[int]]] = {}
        for event_id in fresh:
            number = self._group_of.get(event_id)
            if number is None:
                continue
            ids = overrides.get(event_id)
            if ids is None:
                group = self._groups[number]
                count = bisect_right(group, through_event_id) - int(
                    current_event_id is not None
                    and current_event_id <= through_event_id
                    and self._group_of.get(current_event_id) == number
                )
                anchor = group[0]
            else:
                anchor, count = ids[0], len(ids)
            _, chosen = selected.setdefault(anchor, (count, []))
            chosen.append(event_id)
        grouped: list[EventFragment] = []
        singles: list[EventFragment] = []
        for _, (count, selected_ids) in sorted(selected.items()):
            selected_rows = tuple(fresh[key] for key in selected_ids)
            if len(selected_rows) == count:
                batches, individual = renderer.main_agent_history_views(selected_rows)
                grouped.extend((keys, message) for _, keys, message in batches)
                singles.extend((keys, message) for _, keys, message in individual)
            else:
                for row in selected_rows:
                    message = renderer.reference_message(row)
                    if (message.content or "").strip():
                        fragment = ((row.id,), message)
                        grouped.append(fragment)
                        singles.append(fragment)
        return tuple(grouped), tuple(singles)
