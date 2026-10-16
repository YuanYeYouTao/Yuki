"""Deterministic subject aliases derived from trusted ledger metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel
from qq_ai_bot.memory.enums import MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.extraction import AvailableSubject, MemoryClaim
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
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
    canonical_subject_person_id: str | None = None
    canonical_subject_space_id: str | None = None
    canonical_visibility_person_id: str | None = None
    canonical_visibility_space_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectResolutionContext:
    """One event's model aliases and their backend-owned resolutions."""

    available_subjects: tuple[AvailableSubject, ...]
    resolved_subjects: tuple[tuple[str, ResolvedSubject], ...]
    platform: str | None = None
    space_id: str | None = None

    def resolve(self, subject_ref: str) -> ResolvedSubject | None:
        return next((value for key, value in self.resolved_subjects if key == subject_ref), None)


class SubjectResolver:
    """Map model-visible aliases to backend-owned event subjects."""

    @staticmethod
    def context(event: EventRecord) -> SubjectResolutionContext:
        scopes = [MemoryScopeType.PERSON]
        if event.scope_type is ScopeType.GROUP:
            scopes.append(MemoryScopeType.PERSON_GROUP)
        available = [
            AvailableSubject(
                subject_ref="speaker",
                display_label=(
                    f"{event.sender_group_card or event.sender_nickname or '当前发言者'}"
                    f"（Person {event.author_person_id}）"
                    if event.author_person_id
                    else "当前发言者"
                ),
                allowed_scopes=tuple(scopes),
                relation_to_speaker="self",
            ),
            AvailableSubject(
                subject_ref="self",
                display_label="本 Agent（SELF）",
                allowed_scopes=(MemoryScopeType.SELF,),
                relation_to_speaker="agent_observation",
            ),
        ]
        resolved: list[tuple[str, ResolvedSubject]] = [
            (
                "speaker:person",
                ResolvedSubject(
                    MemoryScopeType.PERSON,
                    event.sender_user_id,
                    None,
                    canonical_subject_person_id=event.author_person_id,
                ),
            )
        ]
        if event.scope_type is ScopeType.GROUP and event.group_id:
            resolved.append(
                (
                    "speaker:person_group",
                    ResolvedSubject(
                        MemoryScopeType.PERSON_GROUP,
                        event.sender_user_id,
                        event.group_id,
                        canonical_subject_person_id=event.author_person_id,
                    ),
                )
            )
            available.append(
                AvailableSubject(
                    subject_ref="group",
                    display_label="当前 Space",
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
            seen = {"", event.author_person_id}
            for member in event.mentioned_person_ids:
                if member in seen:
                    continue
                seen.add(member)
                subject_ref = f"member_{member}"
                available.append(
                    AvailableSubject(
                        subject_ref=subject_ref,
                        display_label=f"Person {member}",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="mentioned_member",
                    )
                )
                resolved.append(
                    (
                        f"{subject_ref}:person_group",
                        ResolvedSubject(
                            MemoryScopeType.PERSON_GROUP,
                            None,
                            event.group_id,
                            canonical_subject_person_id=member,
                        ),
                    )
                )
            reply_author = event.reply_sender_user_id or ""
            if reply_author or event.reply_to_event_id is not None:
                available.append(
                    AvailableSubject(
                        subject_ref="reply_author",
                        display_label="回复消息的发言者",
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
        *,
        database: Database | None = None,
    ) -> None:
        self._people = people
        self._database = database or (people.database if people is not None else None)

    async def resolve_target(self, event: EventRecord, target: ResolvedSubject) -> ResolvedSubject:
        """Keep the event's owner and resolve transport selectors at its real ingress."""
        if self._database is None:
            return target
        async with self._database.sessions() as session:
            conversation = (
                await session.get(CanonicalConversationModel, event.canonical_conversation_id)
                if event.canonical_conversation_id
                else None
            )
            presence = (
                await session.get(PresenceModel, event.ingress_presence_id)
                if event.ingress_presence_id
                else None
            )
            platform = presence.platform if presence is not None else None

            async def person(account: str | None) -> str | None:
                if account == event.sender_user_id:
                    return event.author_person_id
                if account is None or platform is None:
                    return None
                value = await session.scalar(
                    select(IdentityBindingModel.person_id).where(
                        IdentityBindingModel.platform == platform,
                        IdentityBindingModel.external_account_id == account,
                        IdentityBindingModel.status == "active",
                    )
                )
                return str(value) if value is not None else None

            return replace(
                target,
                canonical_subject_person_id=(
                    target.canonical_subject_person_id or await person(target.subject_user_id)
                ),
                canonical_subject_space_id=(
                    target.canonical_subject_space_id or conversation.space_id
                    if conversation is not None
                    and target.scope_type in {MemoryScopeType.GROUP, MemoryScopeType.PERSON_GROUP}
                    else target.canonical_subject_space_id
                ),
                canonical_visibility_person_id=(
                    target.canonical_visibility_person_id or await person(target.visibility_user_id)
                    if target.visibility_type is SelfMemoryVisibility.PRIVATE
                    else target.canonical_visibility_person_id
                ),
                canonical_visibility_space_id=(
                    target.canonical_visibility_space_id or conversation.space_id
                    if conversation is not None
                    and target.visibility_type is SelfMemoryVisibility.GROUP
                    else target.canonical_visibility_space_id
                ),
            )

    async def build(self, event: EventRecord) -> SubjectResolutionContext:
        if (
            self._database is not None
            and not event.mentioned_person_ids
            and event.mentioned_user_ids
            and event.ingress_presence_id
        ):
            async with self._database.sessions() as session:
                presence = await session.get(PresenceModel, event.ingress_presence_id)
                if presence is not None:
                    persons = {
                        account: person_id
                        for account, person_id in await session.execute(
                            select(
                                IdentityBindingModel.external_account_id,
                                IdentityBindingModel.person_id,
                            ).where(
                                IdentityBindingModel.platform == presence.platform,
                                IdentityBindingModel.external_account_id.in_(
                                    event.mentioned_user_ids
                                ),
                                IdentityBindingModel.status == "active",
                            )
                        )
                    }
                    event = replace(
                        event,
                        mentioned_person_ids=tuple(
                            dict.fromkeys(
                                persons[account]
                                for account in event.mentioned_user_ids
                                if account in persons
                            )
                        ),
                    )
        context = SubjectResolver.context(event)
        if self._database is not None and event.reply_to_event_id is not None:
            async with self._database.sessions() as session:
                reply = await session.get(ChatEventModel, event.reply_to_event_id)
                reply_person_id = reply.author_person_id if reply is not None else None
            context = replace(
                context,
                available_subjects=tuple(
                    item
                    for item in context.available_subjects
                    if item.subject_ref != "reply_author" or reply_person_id is not None
                ),
                resolved_subjects=tuple(
                    (ref, replace(target, canonical_subject_person_id=reply_person_id))
                    if ref == "reply_author:person_group"
                    else (ref, target)
                    for ref, target in context.resolved_subjects
                    if ref != "reply_author:person_group" or reply_person_id is not None
                ),
            )
        context = replace(
            context,
            resolved_subjects=tuple(
                [
                    (ref, await self.resolve_target(event, target))
                    for ref, target in context.resolved_subjects
                ]
            ),
        )
        subjects = dict(context.resolved_subjects)
        context = replace(
            context,
            available_subjects=tuple(
                item.model_copy(
                    update={
                        "display_label": (
                            f"Space {target.canonical_subject_space_id}"
                            if target.scope_type is MemoryScopeType.GROUP
                            else f"Person {target.canonical_subject_person_id}"
                        )
                    }
                )
                if item.subject_ref in {"group", "reply_author"}
                and (
                    target := next(
                        (
                            subjects.get(f"{item.subject_ref}:{scope.value}")
                            for scope in item.allowed_scopes
                        ),
                        None,
                    )
                )
                is not None
                else item
                for item in context.available_subjects
            ),
        )
        if self._people is None:
            return context
        user_ids = tuple(
            dict.fromkeys(
                value.subject_user_id for value in subjects.values() if value.subject_user_id
            )
        )
        async with self._people.database.sessions() as session:
            presence = (
                await session.get(PresenceModel, event.ingress_presence_id)
                if event.ingress_presence_id
                else None
            )
            conversation = (
                await session.get(CanonicalConversationModel, event.canonical_conversation_id)
                if event.canonical_conversation_id
                else None
            )
        context = replace(
            context,
            platform=presence.platform if presence is not None else None,
            space_id=conversation.space_id if conversation is not None else None,
        )
        if context.platform is None:
            return context
        person_ids = {
            target.subject_user_id: target.canonical_subject_person_id
            for target in subjects.values()
            if target.subject_user_id and target.canonical_subject_person_id
        }
        profiles = await self._people.get_many(
            user_ids,
            group_id=event.group_id,
            person_ids=person_ids,
            platform=context.platform,
            space_id=context.space_id,
        )
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
                    f"{name}（Person {target.canonical_subject_person_id}）"
                    if name
                    else f"Person {target.canonical_subject_person_id}"
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
        names = tuple(
            dict.fromkeys(
                (claim.subject_name or "").strip()
                for claim in claims
                if claim.subject_ref == "named_member" and (claim.subject_name or "").strip()
            )
        )
        if context.platform is None:
            return claims, context
        matches = await self._people.find_group_members_by_normalized_names(
            names,
            event.group_id,
            platform=context.platform,
            space_id=context.space_id,
        )
        for claim in claims:
            name = (claim.subject_name or "").strip()
            if claim.subject_ref != "named_member" or not name:
                updated.append(claim)
                continue
            exact = matches[name]
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
                        display_label=f"{match.display_name}（Person {match.person_id}）",
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
                            canonical_subject_person_id=match.person_id,
                            canonical_subject_space_id=context.space_id,
                        ),
                    )
                )
            updated.append(claim.model_copy(update={"subject_ref": ref}))
        resolved = [(ref, await self.resolve_target(event, target)) for ref, target in resolved]
        return tuple(updated), replace(
            context, available_subjects=tuple(available), resolved_subjects=tuple(resolved)
        )
