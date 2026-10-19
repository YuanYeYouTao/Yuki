"""Prepare new message text and native targets once, before effects are claimed."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.messages import DISPLAY_NAME_LIMIT, sanitize_display_name
from yuki.gateway.social import PlatformSocialOperations, TransportMention, TransportMessage

_ACCOUNT = re.compile(r"@\{account:([^{}]+)\}")
# Try a mail local part only at its beginning, not every suffix of a long token.
_ADDRESS = re.compile(r"https?://\S+|(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
_LINE_BREAKS = "\r\n\v\f\x85\u2028\u2029"

# CROSS JOIN keeps each lookup driven by the request's names/IDs, not by every
# account on a platform. All interpolated fragments below are fixed SQL here.
_CURRENT_ASSIGNMENT = """
LEFT JOIN account_assignments current
 ON current.account_id = accounts.id AND current.status <> 'ended'
"""
_ACCOUNT_QUERY = sql(
    """
SELECT accounts.id, accounts.external_account_id, accounts.display_name,
 current.id, current.person_id, current.kind
FROM json_each(:accounts) requested
CROSS JOIN accounts ON accounts.id = requested.value
"""
    + _CURRENT_ASSIGNMENT
    + "WHERE accounts.platform = :platform"
)
_GROUP_ACCOUNTS = """
EXISTS (SELECT 1 FROM account_space_profiles profile
 WHERE profile.account_id = accounts.id AND profile.space_binding_id = :scope)
"""
_PRIVATE_ACCOUNTS = """
EXISTS (SELECT 1 FROM json_each(:participants) participant
 CROSS JOIN account_assignments original ON original.id = participant.value
 WHERE original.account_id = accounts.id)
"""
_NAME_QUERY = (
    """
SELECT observation.name, accounts.id, accounts.external_account_id,
 current.id, current.person_id, current.kind
FROM json_each(:names) requested
CROSS JOIN account_name_observations observation
CROSS JOIN accounts ON accounts.id = observation.account_id
"""
    + _CURRENT_ASSIGNMENT
    + """
WHERE observation.name = requested.value AND {scope}
 AND accounts.platform = :platform AND {observed}
"""
)
_GROUP_NAMES = sql(
    _NAME_QUERY.format(scope="observation.space_binding_id = :scope", observed=_GROUP_ACCOUNTS)
    + " UNION ALL "
    + _NAME_QUERY.format(scope="observation.space_binding_id IS NULL", observed=_GROUP_ACCOUNTS)
)
_PRIVATE_NAMES = sql(
    _NAME_QUERY.format(scope="observation.space_binding_id IS NULL", observed=_PRIVATE_ACCOUNTS)
)
_GROUP_CONVERSATION = """
SELECT id FROM canonical_conversations
WHERE kind = 'space' AND space_binding_id = :scope AND thread_id IS NULL
"""
_PRIVATE_CONVERSATION = """
SELECT id FROM canonical_conversations
WHERE kind = 'private' AND thread_id IS NULL
 AND identity_binding_id IN (SELECT value FROM json_each(:participants))
 AND entry_presence_id IN (SELECT value FROM json_each(:participants))
"""
_RECENT_QUERY = """
SELECT accounts.id
FROM json_each(:accounts) requested
CROSS JOIN accounts ON accounts.id = requested.value
LEFT JOIN chat_events recent ON recent.id = (
 SELECT id FROM chat_events
 WHERE canonical_conversation_id = COALESCE(:conversation_id, ({conversation}))
  AND sender_user_id = accounts.external_account_id
  AND suppression_status = 'keeper' AND event_kind = 'message'
 ORDER BY occurred_at DESC, id DESC LIMIT 1
)
ORDER BY recent.occurred_at DESC, recent.id DESC, accounts.id
"""
_GROUP_RECENT = sql(_RECENT_QUERY.format(conversation=_GROUP_CONVERSATION))
_PRIVATE_RECENT = sql(_RECENT_QUERY.format(conversation=_PRIVATE_CONVERSATION))


@dataclass(frozen=True, slots=True)
class NameTarget:
    account_id: str
    external_id: str
    display_name: str
    assignment_id: str | None = None
    person_id: str | None = None
    presence_id: str | None = None

    def mention(self, start: int, end: int) -> TransportMention:
        return TransportMention(
            self.external_id,
            self.display_name,
            start,
            end,
            self.account_id,
            self.assignment_id,
            self.person_id,
            self.presence_id,
            "yuki" if self.presence_id else "member",
        )


@dataclass(frozen=True, slots=True)
class PreparedContent:
    message: TransportMessage
    unresolved: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _At:
    start: int
    name_start: int
    reference_end: int | None = None
    account_id: str | None = None
    escaped: bool = False


def _positions(text: str) -> tuple[_At, ...]:
    """Scan addresses, escapes and internal references once, retaining original spans."""
    addresses = iter(_ADDRESS.finditer(text))
    address = next(addresses, None)
    positions: list[_At] = []
    position = 0
    while position < len(text):
        at = text.find("@", position)
        if at < 0:
            break
        while address is not None and address.end() <= at:
            address = next(addresses, None)
        if address is not None and address.start() <= at < address.end():
            position = address.end()
            continue
        position = at + 1
        if at and text[at - 1] == "\\":
            positions.append(_At(at, position, escaped=True))
            continue
        reference = _ACCOUNT.match(text, at)
        if reference is not None:
            try:
                account_id = str(UUID(reference[1]))
            except ValueError:
                account_id = None
            positions.append(_At(at, position, reference.end(), account_id))
            position = reference.end()
            continue
        while (
            position < len(text) and text[position].isspace() and text[position] not in _LINE_BREAKS
        ):
            position += 1
        positions.append(_At(at, position))
    return tuple(positions)


def _prefixes(text: str, positions: tuple[_At, ...]) -> set[str]:
    tails = {
        text[item.name_start : item.name_start + DISPLAY_NAME_LIMIT]
        for item in positions
        if not item.escaped and item.reference_end is None
    }
    return {tail[:end] for tail in tails for end in range(1, len(tail) + 1)}


async def prepare_for_entry(
    session: AsyncSession,
    text: str,
    *,
    platform: str,
    space_binding_id: str | None,
    participant_assignment_ids: tuple[str, ...] = (),
    operations: PlatformSocialOperations,
    conversation_id: str | None = None,
) -> PreparedContent:
    """Resolve only names present in this text, within the physical entry's accounts."""
    positions = _positions(text)
    exact_ids = {item.account_id for item in positions if item.account_id is not None}
    prefixes = _prefixes(text, positions)
    if not exact_ids and not prefixes:
        return _render_content(text, positions, {}, {}, operations.native_mention, {})
    parameters = {
        "platform": platform,
        "scope": space_binding_id,
        "participants": json.dumps(participant_assignment_ids),
        "conversation_id": conversation_id,
    }
    exact: dict[str, NameTarget] = {}
    if exact_ids:
        rows = await session.execute(
            _ACCOUNT_QUERY, {**parameters, "accounts": json.dumps(tuple(exact_ids))}
        )
        for account_id, external_id, name, assignment_id, person_id, kind in rows:
            exact[account_id] = NameTarget(
                account_id,
                external_id,
                sanitize_display_name(name or "") or "未识别成员",
                assignment_id,
                person_id,
                assignment_id if kind == "yuki" else None,
            )
    names: list[tuple[str, NameTarget]] = []
    if prefixes:
        for name, account_id, external_id, assignment_id, person_id, kind in await session.execute(
            _GROUP_NAMES if space_binding_id is not None else _PRIVATE_NAMES,
            {**parameters, "names": json.dumps(tuple(prefixes), ensure_ascii=False)},
        ):
            names.append(
                (
                    name,
                    NameTarget(
                        account_id,
                        external_id,
                        name,
                        assignment_id,
                        person_id,
                        assignment_id if kind == "yuki" else None,
                    ),
                )
            )
    matches = _name_matches(text, positions, names)
    ambiguous_names = {name: targets for _, name, targets in matches.values() if len(targets) > 1}
    ambiguous = {target.account_id for targets in ambiguous_names.values() for target in targets}
    preference: dict[str, int] = {}
    if ambiguous:
        # One indexed top-1 lookup per truly ambiguous physical account, not a history scan.
        ordered = await session.scalars(
            _GROUP_RECENT if space_binding_id is not None else _PRIVATE_RECENT,
            {**parameters, "accounts": json.dumps(tuple(ambiguous))},
        )
        preference = {account_id: index for index, account_id in enumerate(ordered)}
    return _render_content(text, positions, matches, exact, operations.native_mention, preference)


def _name_matches(
    text: str, positions: tuple[_At, ...], names: Iterable[tuple[str, NameTarget]]
) -> dict[int, tuple[int, str, tuple[NameTarget, ...]]]:
    targets_by_name: dict[str, dict[str, NameTarget]] = {}
    for name, target in names:
        if name:
            targets_by_name.setdefault(name, {})[target.account_id] = target
    trie: dict[Any, Any] = {}
    for name, targets in targets_by_name.items():
        node = trie
        for character in name:
            node = node.setdefault(character, {})
        node[None] = name, tuple(targets.values())
    matches = {}
    consumed = 0
    for item in positions:
        if item.start < consumed or item.escaped or item.reference_end is not None:
            continue
        node, cursor = trie, item.name_start
        while cursor < len(text) and text[cursor] in node:
            node = node[text[cursor]]
            cursor += 1
            if None in node:
                name, targets = node[None]
                matches[item.start] = cursor, name, targets
        if item.start in matches:
            consumed = matches[item.start][0]
    return matches


def _render_content(
    text: str,
    positions: tuple[_At, ...],
    matches: dict[int, tuple[int, str, tuple[NameTarget, ...]]],
    accounts: dict[str, NameTarget],
    native_mention: Callable[[str, int], TransportMention | None] | None,
    preference: dict[str, int],
) -> PreparedContent:
    pieces: list[str] = []
    mentions: list[TransportMention] = []
    unresolved: list[dict[str, object]] = []
    selected: dict[str, NameTarget] = {}
    position = length = 0
    for item in positions:
        if item.start < position:
            continue
        prefix = text[position : item.start - 1 if item.escaped else item.start]
        pieces.append(prefix)
        length += len(prefix)
        if item.escaped:
            literal, position = "@", item.start + 1
        elif item.reference_end is not None:
            target = accounts.get(item.account_id or "")
            if target is not None:
                literal = "@" + target.display_name
                mentions.append(target.mention(length, length + len(literal)))
            else:
                literal = text[item.start : item.reference_end]
                unresolved.append({"text": literal, "error": "account_unavailable"})
            position = item.reference_end
        elif item.start in matches:
            position, name, targets = matches[item.start]
            target = selected.get(name)
            if target is None:
                target = selected[name] = min(
                    targets,
                    key=lambda value: (
                        preference.get(value.account_id, len(preference)),
                        value.account_id,
                    ),
                )
            literal = text[item.start : position]
            mentions.append(target.mention(length, length + len(literal)))
        else:
            native = native_mention(text, item.start) if native_mention else None
            position = native.text_end if native is not None else item.start + 1
            literal = text[item.start : position]
            if native is not None:
                mentions.append(replace(native, text_start=length, text_end=length + len(literal)))
        pieces.append(literal)
        length += len(literal)
    pieces.append(text[position:])
    return PreparedContent(TransportMessage("".join(pieces), tuple(mentions)), tuple(unresolved))
