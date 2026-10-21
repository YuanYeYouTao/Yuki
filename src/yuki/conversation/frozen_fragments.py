"""Immutable, structurally shared copies of submitted public input fragments."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, overload

from yuki.conversation.projections import ProjectionConflict
from yuki.domain.messages import ChatMessage

EventFragment = tuple[tuple[int, ...], ChatMessage]


@dataclass(frozen=True, slots=True)
class FrozenFragment:
    event_ids: tuple[int, ...]
    message: ChatMessage

    def item(self) -> dict[str, Any]:
        """Export an independently mutable persistence DTO at the boundary."""
        return {
            "kind": "model_input",
            "event_ids": list(self.event_ids),
            "message": {"role": self.message.role, "content": self.message.content},
        }


@dataclass(frozen=True, slots=True, eq=False)
class _IdNode:
    key: int
    left: _IdNode | None
    right: _IdNode | None
    height: int
    size: int


def _height(node: _IdNode | None) -> int:
    return node.height if node else 0


def _node(key: int, left: _IdNode | None, right: _IdNode | None) -> _IdNode:
    return _IdNode(
        key,
        left,
        right,
        1 + max(_height(left), _height(right)),
        1 + (left.size if left else 0) + (right.size if right else 0),
    )


def _insert(root: _IdNode | None, key: int) -> _IdNode:
    if root is None:
        return _node(key, None, None)
    if key == root.key:
        return root
    left, right = root.left, root.right
    if key < root.key:
        left = _insert(left, key)
    else:
        right = _insert(right, key)
    if _height(left) > _height(right) + 1:
        assert left is not None
        if _height(left.left) < _height(left.right):
            pivot = left.right
            assert pivot is not None
            left = _node(pivot.key, _node(left.key, left.left, pivot.left), pivot.right)
        return _node(left.key, left.left, _node(root.key, left.right, right))
    if _height(right) > _height(left) + 1:
        assert right is not None
        if _height(right.right) < _height(right.left):
            pivot = right.left
            assert pivot is not None
            right = _node(pivot.key, pivot.left, _node(right.key, pivot.right, right.right))
        return _node(right.key, _node(root.key, left, right.left), right.right)
    return _node(root.key, left, right)


@dataclass(frozen=True, slots=True, eq=False)
class _EventIds(Set[int]):
    """A persistent AVL set; appending IDs never copies an old membership set."""

    root: _IdNode | None = None

    def __len__(self) -> int:
        return self.root.size if self.root else 0

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, int):
            return False
        node = self.root
        while node is not None:
            if value == node.key:
                return True
            node = node.left if value < node.key else node.right
        return False

    def __iter__(self) -> Iterator[int]:
        stack: list[_IdNode] = []
        node = self.root
        while node is not None or stack:
            while node is not None:
                stack.append(node)
                node = node.left
            node = stack.pop()
            yield node.key
            node = node.right

    @classmethod
    def _from_iterable[T](cls, values: Iterable[T]) -> Set[T]:
        return frozenset(values)

    @overload
    def __or__(self, other: Set[int]) -> _EventIds: ...

    @overload
    def __or__[T](self, other: Set[T]) -> Set[int | T]: ...

    def __or__(self, other: Set[Any]) -> Set[Any]:
        if any(not isinstance(value, int) for value in other):
            return frozenset(self) | other
        root = self.root
        for value in other:
            root = _insert(root, value)
        return _EventIds(root)


@dataclass(frozen=True, slots=True, eq=False)
class _Chunk:
    previous: _Chunk | None
    fragments: tuple[FrozenFragment, ...]
    count: int
    characters: int
    first_event_id: int | None


@dataclass(frozen=True, slots=True, eq=False)
class FrozenFragments:
    """Public bytes and IDs are immutable; candidates share their original chunks."""

    _tail: _Chunk | None
    _ids: _EventIds
    _owner: object

    def __len__(self) -> int:
        return self._tail.count if self._tail is not None else 0

    @property
    def fragment_count(self) -> int:
        return len(self)

    @property
    def character_count(self) -> int:
        return self._tail.characters if self._tail is not None else 0

    @property
    def first_event_id(self) -> int | None:
        return self._tail.first_event_id if self._tail is not None else None

    @property
    def items(self) -> tuple[dict[str, Any], ...]:
        """Materialize DTOs only for legacy/persistence boundaries, in O(N)."""
        return tuple(fragment.item() for fragment in self.iter_fragments())

    @property
    def event_ids(self) -> Set[int]:
        return self._ids

    def iter_blocks(self) -> Iterator[tuple[FrozenFragment, ...]]:
        blocks = []
        node = self._tail
        while node is not None:
            blocks.append(node.fragments)
            node = node.previous
        yield from reversed(blocks)

    def iter_fragments(self) -> Iterator[FrozenFragment]:
        for block in self.iter_blocks():
            yield from block

    def delta_from(self, prefix: FrozenFragments) -> tuple[tuple[FrozenFragment, ...], ...]:
        """Return only new chunks of this exact structural ancestor, without DTOs."""
        if self._owner is not prefix._owner:
            raise ProjectionConflict("frozen input has a different owner")
        blocks = []
        node = self._tail
        while node is not prefix._tail:
            if node is None:
                raise ProjectionConflict("frozen input is not an extension of the prefix")
            blocks.append(node.fragments)
            node = node.previous
        return tuple(reversed(blocks))

    def messages(self) -> tuple[ChatMessage, ...]:
        return tuple(fragment.message for fragment in self.iter_fragments())

    def messages_view(self) -> Sequence[ChatMessage]:
        return _Messages(self)

    @classmethod
    def load(cls, items: Iterable[Mapping[str, Any]] | FrozenFragments) -> FrozenFragments:
        if isinstance(items, FrozenFragments):
            return items
        fragments = []
        for item in items:
            if set(item) != {"kind", "event_ids", "message"}:
                raise ProjectionConflict("unsupported frozen fragment schema")
            if item["kind"] != "model_input":
                raise ProjectionConflict("fragment is not a model input")
            ids = item["event_ids"]
            if not isinstance(ids, list):
                raise ProjectionConflict("invalid fragment event identifiers")
            message = item["message"]
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ProjectionConflict("unsupported frozen message representation")
            fragments.append(_freeze(tuple(ids), ChatMessage(**message)))
        return cls(None, _EventIds(), object())._append(tuple(fragments))

    @classmethod
    def from_blocks(cls, blocks: Iterable[tuple[FrozenFragment, ...]]) -> FrozenFragments:
        return cls(None, _EventIds(), object()).extend_blocks(blocks)

    def extend_blocks(self, blocks: Iterable[tuple[FrozenFragment, ...]]) -> FrozenFragments:
        """Attach already frozen storage blocks, retaining this exact prefix owner."""
        frozen = self
        for block in blocks:
            frozen = frozen._append(block)
        return frozen

    def _append(self, fragments: tuple[FrozenFragment, ...]) -> FrozenFragments:
        if not fragments:
            return self
        ids = self._ids.root
        for fragment in fragments:
            for event_id in fragment.event_ids:
                previous = ids
                ids = _insert(ids, event_id)
                if previous is not None and ids.size == previous.size:
                    raise ProjectionConflict("duplicate frozen event")
        return self._attach(fragments, _EventIds(ids))

    def _attach(self, fragments: tuple[FrozenFragment, ...], ids: _EventIds) -> FrozenFragments:
        if not fragments:
            return self
        return FrozenFragments(
            _Chunk(
                self._tail,
                fragments,
                len(self) + len(fragments),
                self.character_count
                + sum(len(fragment.message.content or "") for fragment in fragments),
                self.first_event_id
                or next(
                    (fragment.event_ids[0] for fragment in fragments if fragment.event_ids), None
                ),
            ),
            ids,
            self._owner,
        )

    def extend_history(
        self,
        grouped: tuple[EventFragment, ...],
        individual: tuple[EventFragment, ...],
    ) -> FrozenFragments:
        """Index individual views once; split only the fresh part of overlap groups."""
        covered = self._ids
        additions: list[FrozenFragment] = []
        index: dict[int, list[int]] | None = None
        empty: list[int] = []
        selected: tuple[EventFragment, ...]
        for keys, message in grouped:
            fresh = {key for key in keys if key not in covered}
            if not fresh:
                continue
            if len(fresh) == len(set(keys)):
                selected = ((keys, message),)
            else:
                if index is None:
                    index = {}
                    for ordinal, (ids, _) in enumerate(individual):
                        if not ids:
                            empty.append(ordinal)
                        for key in ids:
                            index.setdefault(key, []).append(ordinal)
                positions = list(empty)
                seen = set(empty)
                for key in keys:
                    if key in fresh:
                        for ordinal in index.get(key, ()):
                            if ordinal not in seen:
                                seen.add(ordinal)
                                positions.append(ordinal)
                if any(left > right for left, right in pairwise(positions)):
                    positions.sort()
                selected = tuple(
                    individual[ordinal]
                    for ordinal in positions
                    if all(key in fresh for key in individual[ordinal][0])
                )
            selected_ids = [key for ids, _ in selected for key in ids]
            if set(selected_ids) != fresh or len(selected_ids) != len(fresh):
                raise ProjectionConflict("new event fragment coverage is incomplete")
            for ids, value in selected:
                additions.append(_freeze(ids, value))
            covered = covered | fresh
        return self._attach(tuple(additions), covered)

    def append_current(self, event_id: int, message: ChatMessage) -> FrozenFragments:
        if event_id in self._ids:
            raise ProjectionConflict("trigger already exists in frozen input")
        return self._append((_freeze((event_id,), message),))


def _freeze(ids: tuple[int, ...], message: ChatMessage) -> FrozenFragment:
    if any(type(value) is not int or value <= 0 for value in ids):
        raise ProjectionConflict("invalid fragment event identifiers")
    if len(set(ids)) != len(ids):
        raise ProjectionConflict("duplicate frozen event")
    if message.role not in {"user", "assistant", "tool", "system"} or (
        message.content is not None and not isinstance(message.content, str)
    ):
        raise ProjectionConflict("unsupported frozen message representation")
    # Pixels, tool calls and opaque protocol are private request material.
    return FrozenFragment(ids, ChatMessage(message.role, message.content))


@dataclass(frozen=True, slots=True, eq=False)
class _Messages(Sequence[ChatMessage]):
    fragments: FrozenFragments

    def __len__(self) -> int:
        return len(self.fragments)

    def __iter__(self) -> Iterator[ChatMessage]:
        return (fragment.message for fragment in self.fragments.iter_fragments())

    @overload
    def __getitem__(self, index: int) -> ChatMessage: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ChatMessage, ...]: ...

    def __getitem__(self, index: int | slice) -> ChatMessage | tuple[ChatMessage, ...]:
        if isinstance(index, slice):
            return tuple(self)[index]
        position = index if index >= 0 else len(self) + index
        if position < 0:
            raise IndexError(index)
        for ordinal, message in enumerate(self):
            if ordinal == position:
                return message
        raise IndexError(index)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence):
            return NotImplemented
        return tuple(self) == tuple(other)
