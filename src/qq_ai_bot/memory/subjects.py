"""Deterministic subject aliases derived from trusted ledger metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.extraction import AvailableSubject, MemoryClaim
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    scope_type: MemoryScopeType
    subject_user_id: str | None
    group_id: str | None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectResolutionContext:
    """One event's model aliases and their backend-owned resolutions."""

    available_subjects: tuple[AvailableSubject, ...]
    resolved_subjects: tuple[tuple[str, ResolvedSubject], ...]

    def resolve(self, subject_ref: str) -> ResolvedSubject | None:
        return next((value for key, value in self.resolved_subjects if key == subject_ref), None)


class SubjectResolver:
    """Map model-visible aliases to backend-owned QQ and group identifiers."""

    @staticmethod
    def context(event: EventRecord) -> SubjectResolutionContext:
        scopes = [MemoryScopeType.PERSON]
        if event.scope_type is ScopeType.GROUP:
            scopes.append(MemoryScopeType.PERSON_GROUP)
        available = [
            AvailableSubject(
                subject_ref="speaker",
                display_label=f"{event.sender_display_name}（ID {event.sender_user_id}）",
                allowed_scopes=tuple(scopes),
                relation_to_speaker="self",
            ),
            AvailableSubject(
                subject_ref="self",
                display_label=f"本 Agent（ID {event.bot_user_id}）",
                allowed_scopes=(MemoryScopeType.SELF,),
                relation_to_speaker="agent_observation",
            ),
        ]
        resolved: list[tuple[str, ResolvedSubject]] = [
            ("speaker:person", ResolvedSubject(MemoryScopeType.PERSON, event.sender_user_id, None))
        ]
        if event.scope_type is ScopeType.GROUP and event.group_id:
            resolved.append(
                (
                    "speaker:person_group",
                    ResolvedSubject(
                        MemoryScopeType.PERSON_GROUP,
                        event.sender_user_id,
                        event.group_id,
                    ),
                )
            )
            available.append(
                AvailableSubject(
                    subject_ref="group",
                    display_label=f"群 {event.group_id}",
                    allowed_scopes=(MemoryScopeType.GROUP,),
                    relation_to_speaker="current_group",
                )
            )
            available.append(
                AvailableSubject(
                    subject_ref="named_member",
                    display_label="模型明确给出的当前群成员姓名",
                    allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                    relation_to_speaker="current_group_name_requires_resolution",
                )
            )
            resolved.append(
                ("group:group", ResolvedSubject(MemoryScopeType.GROUP, None, event.group_id))
            )
            seen = {"", event.sender_user_id}
            for user_id in event.mentioned_user_ids:
                if user_id in seen:
                    continue
                seen.add(user_id)
                subject_ref = f"member_{user_id}"
                available.append(
                    AvailableSubject(
                        subject_ref=subject_ref,
                        display_label=f"ID {user_id}",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="mentioned_member",
                    )
                )
                resolved.append(
                    (
                        f"{subject_ref}:person_group",
                        ResolvedSubject(MemoryScopeType.PERSON_GROUP, user_id, event.group_id),
                    )
                )
            reply_author = event.reply_sender_user_id or ""
            if reply_author:
                available.append(
                    AvailableSubject(
                        subject_ref="reply_author",
                        display_label=f"ID {reply_author}",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="reply_author",
                    )
                )
                resolved.append(
                    (
                        "reply_author:person_group",
                        ResolvedSubject(
                            MemoryScopeType.PERSON_GROUP,
                            reply_author,
                            event.group_id,
                        ),
                    )
                )
        return SubjectResolutionContext(tuple(available), tuple(resolved))

    @staticmethod
    def available(event: EventRecord) -> tuple[AvailableSubject, ...]:
        return SubjectResolver.context(event).available_subjects

    @staticmethod
    def resolve(
        event: EventRecord,
        *,
        subject_ref: str,
        scope_type: MemoryScopeType,
        context: SubjectResolutionContext | None = None,
    ) -> ResolvedSubject | None:
        if subject_ref == "self" and scope_type is MemoryScopeType.SELF:
            return ResolvedSubject(scope_type, None, None)
        selected = context or SubjectResolver.context(event)
        return selected.resolve(f"{subject_ref}:{scope_type.value}")


class SubjectContextBuilder:
    """Resolve only names explicitly declared by the extraction model."""

    def __init__(
        self,
        people: PeopleRepository | None = None,
    ) -> None:
        self._people = people

    async def build(self, event: EventRecord) -> SubjectResolutionContext:
        context = SubjectResolver.context(event)
        if self._people is None:
            return context
        subjects = dict(context.resolved_subjects)
        user_ids = tuple(
            dict.fromkeys(
                value.subject_user_id for value in subjects.values() if value.subject_user_id
            )
        )
        profiles = await self._people.get_many(user_ids, group_id=event.group_id)
        available = []
        for item in context.available_subjects:
            target = next(
                (
                    subjects.get(f"{item.subject_ref}:{scope.value}")
                    for scope in item.allowed_scopes
                ),
                None,
            )
            if target is not None and target.subject_user_id is not None:
                profile = profiles.get(target.subject_user_id)
                name = (
                    event.sender_group_card or event.sender_nickname
                    if target.subject_user_id == event.sender_user_id
                    else profile.group_card or profile.nickname
                    if profile
                    else ""
                )
                label = (
                    f"{name}（ID {target.subject_user_id}）"
                    if name
                    else f"ID {target.subject_user_id}"
                )
                item = item.model_copy(update={"display_label": label})
            available.append(item)
        return replace(context, available_subjects=tuple(available))

    async def resolve_claim_names(
        self,
        event: EventRecord,
        claims: tuple[MemoryClaim, ...],
        context: SubjectResolutionContext,
    ) -> tuple[tuple[MemoryClaim, ...], SubjectResolutionContext]:
        if self._people is None or event.scope_type is not ScopeType.GROUP or not event.group_id:
            return claims, context
        available = list(context.available_subjects)
        resolved = list(context.resolved_subjects)
        refs_by_user: dict[str, str] = {}
        updated: list[MemoryClaim] = []
        for claim in claims:
            name = (claim.subject_name or "").strip()
            if claim.subject_ref != "named_member" or not name:
                updated.append(claim)
                continue
            matches = await self._people.search_group_member_names(
                name,
                event.group_id,
                minimum_score=0.35,
            )
            exact = tuple(item for item in matches if item.exact)
            if len(exact) != 1:
                updated.append(claim)
                continue
            match = exact[0]
            ref = refs_by_user.get(match.user_id)
            if ref is None:
                ref = f"named_{len(refs_by_user) + 1}"
                refs_by_user[match.user_id] = ref
                available.append(
                    AvailableSubject(
                        subject_ref=ref,
                        display_label=f"{match.display_name}（ID {match.user_id}）",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="unique_group_name",
                    )
                )
                resolved.append(
                    (
                        f"{ref}:person_group",
                        ResolvedSubject(
                            MemoryScopeType.PERSON_GROUP,
                            match.user_id,
                            event.group_id,
                        ),
                    )
                )
            updated.append(claim.model_copy(update={"subject_ref": ref}))
        return tuple(updated), SubjectResolutionContext(tuple(available), tuple(resolved))
