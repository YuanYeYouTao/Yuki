"""Prepare new message text and native targets once, before effects are claimed."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.messages import sanitize_display_name
from yuki.gateway.social import PlatformSocialOperations, TransportMention, TransportMessage
from yuki.identity.db_models import (
    AccountAssignmentModel,
    AccountModel,
    AccountNameObservationModel,
    AccountSpaceProfileModel,
)

_ACCOUNT = re.compile(r"@\{account:([^}]+)\}")
_ADDRESS = re.compile(r"https?://\S+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")


@dataclass(frozen=True, slots=True)
class NameTarget:
    account_id: str
    external_id: str
    display_name: str
    assignment_id: str | None = None
    person_id: str | None = None
    presence_id: str | None = None
    ended: bool = False

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


async def prepare_for_entry(
    session: AsyncSession,
    text: str,
    *,
    platform: str,
    space_binding_id: str | None,
    participant_assignment_ids: tuple[str, ...] = (),
    operations: PlatformSocialOperations,
) -> PreparedContent:
    """Read only this physical entry's account facts, without contact admission."""
    exact_ids = account_references(text)
    natural = has_natural_mentions(text)
    if not exact_ids and not natural:
        return prepare_content(text)
    exact = (
        {
            row.id: NameTarget(row.id, row.external_account_id, row.display_name or "未识别成员")
            for row in await session.scalars(
                select(AccountModel).where(
                    AccountModel.id.in_(exact_ids), AccountModel.platform == platform
                )
            )
        }
        if exact_ids
        else {}
    )
    names: list[tuple[str, NameTarget]] = []
    if natural:
        observed = (
            select(AccountSpaceProfileModel.account_id).where(
                AccountSpaceProfileModel.space_binding_id == space_binding_id
            )
            if space_binding_id is not None
            else select(AccountAssignmentModel.account_id).where(
                AccountAssignmentModel.id.in_(participant_assignment_ids)
            )
        )
        rows = (
            await session.execute(
                select(AccountNameObservationModel, AccountModel, AccountAssignmentModel)
                .join(AccountModel, AccountModel.id == AccountNameObservationModel.account_id)
                .outerjoin(
                    AccountAssignmentModel,
                    AccountAssignmentModel.id == AccountNameObservationModel.assignment_id,
                )
                .where(
                    AccountModel.id.in_(observed),
                    AccountModel.platform == platform,
                    or_(
                        AccountNameObservationModel.space_binding_id.is_(None),
                        AccountNameObservationModel.space_binding_id == space_binding_id,
                    ),
                )
            )
        ).all()
        for observation, account, assignment in rows:
            name = sanitize_display_name(observation.name)
            names.append(
                (
                    name,
                    NameTarget(
                        account.id,
                        account.external_account_id,
                        name,
                        assignment.id if assignment else None,
                        assignment.person_id if assignment else None,
                        assignment.id if assignment and assignment.kind == "yuki" else None,
                        assignment.status == "ended" if assignment else False,
                    ),
                )
            )
    return prepare_content(
        text, names=names, accounts=exact, native_mention=operations.native_mention
    )


async def validate_mentions(
    session: AsyncSession, mentions: tuple[TransportMention, ...], platform: str
) -> None:
    """A recipient is not an actor; only prepared identity facts can change."""
    staged = {item.assignment_id: item for item in mentions if item.assignment_id is not None}
    if not staged:
        return
    rows = (
        await session.execute(
            select(AccountAssignmentModel, AccountModel.platform)
            .join(AccountModel, AccountModel.id == AccountAssignmentModel.account_id)
            .where(AccountAssignmentModel.id.in_(staged))
        )
    ).all()
    actual = {assignment.id: (assignment, native_platform) for assignment, native_platform in rows}
    for assignment_id, mention in staged.items():
        found = actual.get(assignment_id)
        if (
            found is None
            or found[0].status == "ended"
            or found[0].account_id != mention.account_id
            or found[0].person_id != mention.person_id
            or found[1] != platform
        ):
            from yuki.social.models import SocialError

            raise SocialError("mention_assignment_changed")


def account_references(text: str) -> tuple[str, ...]:
    """Only validated internal references become a batch account lookup."""
    result: dict[str, None] = {}
    for match in _ACCOUNT.finditer(text):
        try:
            result[str(UUID(match[1]))] = None
        except ValueError:
            continue
    return tuple(result)


def has_natural_mentions(text: str) -> bool:
    without_refs = _ACCOUNT.sub("", text)
    without_addresses = _ADDRESS.sub("", without_refs)
    return "@" in without_addresses.replace("\\@", "")


def prepare_content(
    text: str,
    *,
    names: Iterable[tuple[str, NameTarget]] = (),
    accounts: dict[str, NameTarget] | None = None,
    native_mention: Callable[[str, int], TransportMention | None] | None = None,
) -> PreparedContent:
    """Match known prefixes locally; ambiguity preserves the spoken words."""
    accounts = accounts or {}
    trie: dict[Any, Any] = {}
    for name, target in names:
        if not name:
            continue
        node = trie
        for character in name:
            node = node.setdefault(character, {})
        node.setdefault(None, {})[(target.account_id, target.assignment_id)] = target
    protected = iter(_ADDRESS.finditer(text))
    address = next(protected, None)
    pieces: list[str] = []
    mentions: list[TransportMention] = []
    unresolved: list[dict[str, object]] = []
    position = length = 0
    while position < len(text):
        at = text.find("@", position)
        if at < 0:
            pieces.append(text[position:])
            break
        while address is not None and address.end() <= at:
            address = next(protected, None)
        if address is not None and address.start() <= at < address.end():
            literal = text[position : address.end()]
            pieces.append(literal)
            length += len(literal)
            position = address.end()
            continue
        escaped = at > position and text[at - 1] == "\\"
        prefix = text[position : at - 1] if escaped else text[position:at]
        pieces.append(prefix)
        length += len(prefix)
        if escaped:
            pieces.append("@")
            length += 1
            position = at + 1
            continue
        explicit = _ACCOUNT.match(text, at)
        if explicit is not None:
            try:
                account_id = str(UUID(explicit[1]))
            except ValueError:
                account_id = ""
            explicit_target = accounts.get(account_id)
            if explicit_target is not None:
                label = "@" + explicit_target.display_name
                pieces.append(label)
                mentions.append(explicit_target.mention(length, length + len(label)))
                length += len(label)
            else:
                label = explicit[0].removeprefix("@")
                pieces.append(label)
                length += len(label)
                unresolved.append({"text": explicit[0], "error": "account_unavailable"})
            position = explicit.end()
            continue
        node = trie
        cursor = at + 1
        matches: dict[tuple[str, str | None], tuple[NameTarget, int]] = {}
        while cursor < len(text) and text[cursor] in node:
            node = node[text[cursor]]
            cursor += 1
            for key, target in node.get(None, {}).items():
                matches[key] = (target, cursor)
        if matches:
            end = max(value[1] for value in matches.values())
            if len(matches) == 1 and not next(iter(matches.values()))[0].ended:
                target, end = next(iter(matches.values()))
                literal = text[at:end]
                mentions.append(target.mention(length, length + len(literal)))
            else:
                literal = text[at + 1 : end]
                unresolved.append(
                    {
                        "text": text[at:end],
                        "error": "name_ambiguous" if len(matches) > 1 else "assignment_ended",
                        "candidates": [
                            {
                                "account_id": target.account_id,
                                "reference": f"@{{account:{target.account_id}}}",
                            }
                            for target, _ in matches.values()
                        ],
                    }
                )
            pieces.append(literal)
            length += len(literal)
            position = end
            continue
        native = native_mention(text, at) if native_mention else None
        if native is not None:
            literal = text[at : native.text_end]
            pieces.append(literal)
            mentions.append(replace(native, text_start=length, text_end=length + len(literal)))
            length += len(literal)
            position = native.text_end
            continue
        pieces.append("@")
        length += 1
        position = at + 1
    return PreparedContent(TransportMessage("".join(pieces), tuple(mentions)), tuple(unresolved))
