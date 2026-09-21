"""Repositories for people, memberships, groups, and private access."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.hydrate import delete_canonical_rollup_projections
from qq_ai_bot.domain.audio import parse_transcripts, serialize_transcripts
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.emoji.db_models import EmojiAssetModel, EmojiUsageEventModel
from qq_ai_bot.identity.canonical_repository import (
    AccountRole,
    bindings_for_person,
    external_id,
    find_identity_binding,
    find_presence,
    forget_person_for_external_account,
    trip,
)
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.person_state import (
    load_canonical_aliases,
    load_canonical_group,
    load_canonical_group_member_name_projections,
    load_canonical_group_members_by_exact_name,
    load_canonical_members_in_group,
    load_canonical_membership_count,
    load_canonical_people_by_exact_name,
    load_canonical_person_enabled,
    load_canonical_profile,
    observe_canonical_person,
    observe_canonical_space,
    set_canonical_person_enabled,
    set_canonical_space_flags,
)
from qq_ai_bot.identity.write_settings import identity_write_settings
from qq_ai_bot.memory.dream.db_models import MemoryDreamClusterModel
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    AutomationModel,
    ChatEventModel,
    MembershipModel,
    MemoryFactModel,
    MemoryJobModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
    PersonAliasModel,
    PersonRelationshipModel,
    PersonTimeSettingModel,
    RelationshipEventModel,
    RelationshipJobModel,
    RuntimeConfigOverrideModel,
    RuntimeTurnObservationModel,
    ToolInvocationModel,
    WebSearchRunModel,
)
from qq_ai_bot.persistence.repository_records import (
    GroupSetting,
    PrivateUserSetting,
)
from qq_ai_bot.plugin_host.db_models import (
    PluginAgentMessageModel,
    PluginAgentSessionModel,
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginConfigValueModel,
    PluginNotificationOutboxModel,
    PluginStateModel,
)
from qq_ai_bot.runtime.observability import hash_conversation_key
from qq_ai_bot.speech.db_models import PersonSpeechPreferenceModel, SpeechGenerationModel


@dataclass(frozen=True, slots=True)
class GroupMemberNameMatch:
    user_id: str
    nickname: str
    group_card: str
    matched_alias: str
    score: float
    exact: bool

    @property
    def display_name(self) -> str:
        return self.group_card or self.nickname or self.user_id


def normalize_person_name(value: str) -> str:
    """Normalize identity labels without inferring any linguistic role."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if unicodedata.category(character)[0] in {"L", "N"}
    )


def _score_group_member_identities(
    identities: dict[str, tuple[str, str, set[str]]],
    query: str,
    *,
    limit: int,
    minimum_score: float,
) -> tuple[GroupMemberNameMatch, ...]:
    matches: list[GroupMemberNameMatch] = []
    for user_id, (nickname, group_card, aliases) in identities.items():
        labels = tuple(dict.fromkeys((group_card, nickname, *sorted(aliases))))
        best_alias = ""
        best_score = 0.0
        best_exact = False
        for label in labels:
            candidate = normalize_person_name(label)
            if not candidate:
                continue
            exact = candidate == query
            if exact:
                score = 1.0
            elif query in candidate or candidate in query:
                score = 0.75 + 0.25 * (
                    min(len(query), len(candidate)) / max(len(query), len(candidate))
                )
            else:
                score = SequenceMatcher(None, query, candidate).ratio()
            if score > best_score or (
                score == best_score and label.casefold() < best_alias.casefold()
            ):
                best_alias = label
                best_score = score
                best_exact = exact
        if best_alias and best_score >= minimum_score:
            matches.append(
                GroupMemberNameMatch(
                    user_id=user_id,
                    nickname=nickname,
                    group_card=group_card,
                    matched_alias=best_alias,
                    score=best_score,
                    exact=best_exact,
                )
            )
    matches.sort(
        key=lambda item: (
            -item.score,
            item.display_name.casefold(),
            item.user_id,
        )
    )
    return tuple(matches[: min(limit, 5)])


async def _observe_role(
    session: AsyncSession,
    user_id: str,
    *,
    is_bot: bool,
) -> AccountRole:
    settings = identity_write_settings()
    if is_bot or user_id in settings.ignored_bot_users:
        return "external_bot"
    external = external_id(user_id)
    if external in settings.ignored_bot_users:
        return "external_bot"
    presence = await find_presence(session, external)
    binding = await find_identity_binding(session, external)
    if presence is not None and binding is None:
        return "yuki_self"
    return "human"


async def _canonical_conversation_for_privacy_scope(
    session: AsyncSession,
    scope: ConversationScope,
) -> CanonicalConversationModel | None:
    alias = await session.scalar(
        select(ConversationLegacyAliasModel).where(
            ConversationLegacyAliasModel.scope_key == scope.key
        )
    )
    if alias is None:
        return None
    return await session.get(CanonicalConversationModel, alias.conversation_id)


class PeopleRepository:
    """Keep one global person and exact per-group memberships."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
        memory_rebuilds: MemoryRebuildRepository | None = None,
    ) -> None:
        self._database = database
        self._initial_affection = initial_affection
        self._initial_trust = initial_trust
        self._memory_rebuilds = memory_rebuilds

    @property
    def database(self) -> Database:
        """Canonical storage for host-owned identity read policies."""
        return self._database

    async def affected_conversation_scopes(
        self,
        user_id: str,
    ) -> tuple[ConversationScope, ...]:
        """Resolve every conversation whose retained projection may mention a person."""

        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        ChatEventModel.bot_user_id,
                        ChatEventModel.scope_type,
                        ChatEventModel.group_id,
                        ChatEventModel.private_peer_user_id,
                    )
                    .where(
                        or_(
                            ChatEventModel.sender_user_id == user_id,
                            ChatEventModel.private_peer_user_id == user_id,
                            ChatEventModel.content.contains(user_id),
                            ChatEventModel.visual_summary.contains(user_id),
                            ChatEventModel.audio_transcript.contains(user_id),
                            ChatEventModel.segments_json.contains(user_id),
                        )
                    )
                    .distinct()
                )
            ).all()
        scopes: dict[str, ConversationScope] = {}
        for bot_user_id, scope_type, group_id, private_peer_user_id in rows:
            if scope_type == ScopeType.GROUP.value and group_id is not None:
                scope = ConversationScope.group(str(bot_user_id), str(group_id))
            elif private_peer_user_id is not None:
                scope = ConversationScope.private(
                    str(bot_user_id),
                    str(private_peer_user_id),
                )
            else:
                continue
            scopes[scope.key] = scope
        return tuple(scopes[key] for key in sorted(scopes))

    async def observe(
        self,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        group_name: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        is_bot: bool = False,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> None:
        """Update current values and retain historical aliases."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await observe_canonical_person(
                session,
                user_id=user_id,
                nickname=nickname if nickname_known else "",
                group_id=group_id,
                group_card=group_card,
                group_name=group_name,
                nickname_known=nickname_known,
                group_card_known=group_card_known,
                role=await _observe_role(session, user_id, is_bot=is_bot),
                initial_affection=(
                    self._initial_affection if initial_affection is None else initial_affection
                ),
                initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
                now=now,
            )

    async def get(self, *, user_id: str, group_id: str | None = None) -> UserProfileSnapshot | None:
        async with self._database.sessions() as session:
            loaded = await load_canonical_profile(session, user_id=user_id, group_id=group_id)
            if loaded is None:
                return None
            nickname, card = loaded
            return UserProfileSnapshot(
                user_id=user_id,
                scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                nickname=nickname,
                group_id=group_id,
                group_card=card,
            )

    async def get_many(
        self,
        user_ids: tuple[str, ...],
        *,
        group_id: str | None = None,
    ) -> dict[str, UserProfileSnapshot]:
        """Load several people and their current-group cards in one query."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        async with self._database.sessions() as session:
            loaded: dict[str, UserProfileSnapshot] = {}
            for item in unique_ids:
                pair = await load_canonical_profile(session, user_id=item, group_id=group_id)
                if pair is None:
                    continue
                nickname, card = pair
                loaded[item] = UserProfileSnapshot(
                    user_id=item,
                    scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                    nickname=nickname,
                    group_id=group_id,
                    group_card=card,
                )
            return loaded

    async def aliases(self, user_id: str, *, limit: int = 20) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            return await load_canonical_aliases(session, user_id, limit=limit)

    async def membership_count(self, user_id: str) -> int:
        async with self._database.sessions() as session:
            return await load_canonical_membership_count(session, user_id)

    async def members_in_group(
        self,
        user_ids: tuple[str, ...],
        group_id: str,
    ) -> frozenset[str]:
        """Return only identities with a real membership in the exact group."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return frozenset()
        async with self._database.sessions() as session:
            return await load_canonical_members_in_group(session, unique_ids, group_id)

    async def person_reference_ids(
        self,
        user_ids: tuple[str, ...],
        *,
        speaker_user_id: str,
        bot_user_id: str,
    ) -> tuple[str, ...]:
        """Person mention/reply targets only. Yuki/external never reach membership."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return ()
        async with self._database.sessions() as session:
            from qq_ai_bot.identity.event_author import canonical_person_reference_ids

            return await canonical_person_reference_ids(
                session, unique_ids, speaker_user_id=speaker_user_id
            )

    async def find_group_members_by_exact_name(
        self,
        name: str,
        group_id: str,
    ) -> tuple[str, ...]:
        """Resolve an exact nickname, group card, or in-scope alias inside one group."""

        normalized = name.strip()
        if not normalized:
            return ()
        async with self._database.sessions() as session:
            return await load_canonical_group_members_by_exact_name(session, normalized, group_id)

    async def search_group_member_names(
        self,
        name: str,
        group_id: str,
        *,
        limit: int = 5,
        minimum_score: float = 0.35,
    ) -> tuple[GroupMemberNameMatch, ...]:
        """Return deterministic current-group identity candidates for a model-supplied name."""

        query = normalize_person_name(name)
        if not query or limit <= 0:
            return ()
        async with self._database.sessions() as session:
            projections = await load_canonical_group_member_name_projections(session, group_id)
            projected = {
                item.user_id: (item.nickname, item.group_card, set(item.aliases))
                for item in projections
            }
            return _score_group_member_identities(
                projected,
                query,
                limit=limit,
                minimum_score=minimum_score,
            )

    async def find_people_by_exact_name(self, name: str) -> tuple[str, ...]:
        """Resolve one exact nickname or historical alias across all conversations."""

        normalized = name.strip()
        if not normalized:
            return ()
        async with self._database.sessions() as session:
            return await load_canonical_people_by_exact_name(session, normalized)

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(
                    user_id,
                    enabled,
                    initial_affection=initial_affection,
                    initial_trust=initial_trust,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        return await set_canonical_person_enabled(session, user_id, enabled, now=now)

    async def get_enabled(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting | None:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get_enabled(user_id, session=owned_session)
        return await load_canonical_person_enabled(session, user_id)

    async def delete_person(self, user_id: str, *, marker: str = "[已删除用户]") -> bool:
        """Delete all attributable data and redact exact QQ text elsewhere."""

        async with self._database.immediate_session() as session:
            return await self._delete_person_canonical(session, user_id, marker=marker)

    async def _delete_person_canonical(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        marker: str,
    ) -> bool:
        external = external_id(user_id)
        binding = await find_identity_binding(session, external)
        if binding is None:
            return False
        person_id = binding.person_id
        owner_externals = tuple(
            dict.fromkeys(
                item.external_account_id for item in await bindings_for_person(session, person_id)
            )
        )
        if not owner_externals:
            raise CanonicalIdentityError("unclassified")
        affected_scopes = await self._affected_scopes_for_owners(session, owner_externals)
        privacy_event_match = or_(
            ChatEventModel.sender_user_id.in_(owner_externals),
            ChatEventModel.private_peer_user_id.in_(owner_externals),
            *[ChatEventModel.content.contains(item) for item in owner_externals],
            *[ChatEventModel.visual_summary.contains(item) for item in owner_externals],
            *[ChatEventModel.audio_transcript.contains(item) for item in owner_externals],
            *[ChatEventModel.segments_json.contains(item) for item in owner_externals],
        )
        private_scope_keys = tuple(
            scope.key
            for scope in affected_scopes
            if (
                scope.scope_type is ScopeType.PRIVATE
                and scope.private_peer_user_id in owner_externals
            )
        )
        web_cleanup_conditions = [
            WebSearchRunModel.trigger_event_id.in_(
                select(ChatEventModel.id).where(privacy_event_match)
            ),
            *[WebSearchRunModel.conversation_key == f"private:{item}" for item in owner_externals],
            *[
                WebSearchRunModel.conversation_key.like(f"group:%:user:{item}")
                for item in owner_externals
            ],
        ]
        if private_scope_keys:
            web_cleanup_conditions.append(
                WebSearchRunModel.conversation_key.in_(private_scope_keys)
            )
        await session.execute(delete(WebSearchRunModel).where(or_(*web_cleanup_conditions)))
        now = datetime.now(UTC)
        for scope in affected_scopes:
            conversation = await _canonical_conversation_for_privacy_scope(session, scope)
            is_private_target = (
                scope.scope_type is ScopeType.PRIVATE
                and scope.private_peer_user_id in owner_externals
            )
            if conversation is not None:
                from qq_ai_bot.runtime.work_repository import WorkRepository

                await WorkRepository.purge_scope(session, conversation.id)
                await delete_canonical_rollup_projections(session, conversation.id)
                if not is_private_target:
                    conversation.generation += 1
                    conversation.starts_after_event_id = conversation.last_event_id
                    conversation.covered_through_event_id = conversation.last_event_id
                    conversation.last_generation_change_event_id = 0
                    conversation.uncovered_event_count = 0
                    conversation.uncovered_character_count = 0
                    conversation.revision += 1
                    conversation.updated_at = now
        await self._forget_person_plugin(session, person_id, owner_externals)
        trip("after_forget_plugin")
        await self._forget_person_automations(session, person_id)
        trip("after_forget_automations")
        await self._forget_person_memory(session, person_id)
        trip("after_forget_memory")
        await self._forget_person_conversation(session, person_id, owner_externals)
        trip("after_forget_conversation")
        if self._memory_rebuilds is not None:
            for owner in owner_externals:
                await self._memory_rebuilds.forget_person(owner, session=session)
        remaining = (
            await session.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.sender_user_id.notin_(owner_externals),
                    or_(
                        ChatEventModel.private_peer_user_id.is_(None),
                        ChatEventModel.private_peer_user_id.notin_(owner_externals),
                    ),
                    or_(
                        *[ChatEventModel.content.contains(item) for item in owner_externals],
                        *[ChatEventModel.visual_summary.contains(item) for item in owner_externals],
                        *[
                            ChatEventModel.audio_transcript.contains(item)
                            for item in owner_externals
                        ],
                        *[ChatEventModel.segments_json.contains(item) for item in owner_externals],
                    ),
                )
            )
        ).all()
        for event in remaining:
            for owner in owner_externals:
                event.content = event.content.replace(owner, marker)
                event.visual_summary = event.visual_summary.replace(owner, marker)
                event.audio_transcript = serialize_transcripts(
                    tuple(
                        replace(item, text=item.text.replace(owner, marker))
                        for item in parse_transcripts(event.audio_transcript)
                    )
                )
                event.segments_json = event.segments_json.replace(owner, marker)
        await session.execute(
            delete(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user",
                RuntimeConfigOverrideModel.canonical_person_id == person_id,
            )
        )
        for owner in owner_externals:
            await session.execute(
                update(RuntimeConfigOverrideModel)
                .where(RuntimeConfigOverrideModel.updated_by == owner)
                .values(updated_by=marker)
            )
        audit_match = or_(
            AdminOperationEventModel.actor_user_id.in_(owner_externals),
            AdminOperationEventModel.target_id.in_(owner_externals),
            *[AdminOperationEventModel.conversation_key.contains(item) for item in owner_externals],
            *[AdminOperationEventModel.before_json.contains(item) for item in owner_externals],
            *[AdminOperationEventModel.after_json.contains(item) for item in owner_externals],
        )
        audit_rows = (
            await session.scalars(select(AdminOperationEventModel).where(audit_match))
        ).all()
        for audit in audit_rows:
            for owner in owner_externals:
                if audit.actor_user_id == owner:
                    audit.actor_user_id = marker
                if audit.target_id == owner:
                    audit.target_id = marker
                audit.conversation_key = audit.conversation_key.replace(owner, marker)
                audit.before_json = audit.before_json.replace(owner, marker)
                audit.after_json = audit.after_json.replace(owner, marker)
        await session.execute(
            delete(EmojiUsageEventModel).where(
                or_(
                    EmojiUsageEventModel.canonical_actor_person_id == person_id,
                    EmojiUsageEventModel.actor_user_id.in_(owner_externals),
                )
            )
        )
        await session.execute(
            update(EmojiAssetModel)
            .where(
                or_(
                    EmojiAssetModel.canonical_first_seen_person_id == person_id,
                    EmojiAssetModel.first_seen_user_id.in_(owner_externals),
                )
            )
            .values(
                canonical_first_seen_person_id=None,
                first_seen_user_id=None,
            )
        )
        await session.execute(
            delete(RelationshipJobModel).where(
                RelationshipJobModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(RelationshipEventModel).where(
                RelationshipEventModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(PersonRelationshipModel).where(
                PersonRelationshipModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(PersonSpeechPreferenceModel).where(
                PersonSpeechPreferenceModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(PersonTimeSettingModel).where(
                PersonTimeSettingModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(ChatEventModel).where(
                or_(
                    ChatEventModel.sender_user_id.in_(owner_externals),
                    ChatEventModel.private_peer_user_id.in_(owner_externals),
                )
            )
        )
        await session.execute(
            delete(MembershipModel).where(
                MembershipModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(PersonAliasModel).where(
                PersonAliasModel.canonical_person_id == person_id,
            )
        )
        await forget_person_for_external_account(session, user_id)
        return True

    @staticmethod
    def _queued_target_belongs_to_forgotten_person(
        row: PluginNotificationOutboxModel | PluginBackgroundTurnJobModel,
        person_id: str,
        space_keys: set[tuple[str, str]],
    ) -> bool:
        """Match queues by their canonical target ownership."""

        if row.canonical_target_person_id == person_id:
            return True
        space_id = row.canonical_target_space_id
        if space_id is not None and (str(row.plugin_id), str(space_id)) in space_keys:
            return True
        return False

    @staticmethod
    async def _forget_person_plugin(
        session: AsyncSession,
        person_id: str,
        owner_externals: tuple[str, ...],
    ) -> None:
        """Delete Person-owned plugin rows before the generic shadow-null loop.

        Space/global content stays unless a P-created grant attributes it.
        Q-created grants targeting the same Space are preserved.
        """

        space_grant_rows = (
            await session.execute(
                select(
                    PluginBackgroundTargetGrantModel.plugin_id,
                    PluginBackgroundTargetGrantModel.canonical_target_space_id,
                ).where(
                    PluginBackgroundTargetGrantModel.canonical_created_by_person_id == person_id,
                    PluginBackgroundTargetGrantModel.canonical_target_space_id.is_not(None),
                )
            )
        ).all()
        space_keys = {
            (str(plugin_id), str(space_id))
            for plugin_id, space_id in space_grant_rows
            if plugin_id and space_id
        }
        outbox_rows = (await session.scalars(select(PluginNotificationOutboxModel))).all()
        job_rows = (await session.scalars(select(PluginBackgroundTurnJobModel))).all()
        for row in outbox_rows:
            if PeopleRepository._queued_target_belongs_to_forgotten_person(
                row, person_id, space_keys
            ):
                await session.delete(row)
        for job in job_rows:
            if PeopleRepository._queued_target_belongs_to_forgotten_person(
                job, person_id, space_keys
            ):
                await session.delete(job)
        await session.execute(
            delete(PluginBackgroundTargetGrantModel).where(
                or_(
                    PluginBackgroundTargetGrantModel.canonical_created_by_person_id == person_id,
                    PluginBackgroundTargetGrantModel.canonical_target_person_id == person_id,
                    PluginBackgroundTargetGrantModel.created_by_user_id.in_(owner_externals),
                )
            )
        )
        await session.execute(
            delete(PluginStateModel).where(
                PluginStateModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(PluginConfigValueModel).where(
                PluginConfigValueModel.scope_type == "user",
                PluginConfigValueModel.canonical_person_id == person_id,
            )
        )
        await session.execute(
            delete(PluginAgentSessionModel).where(
                PluginAgentSessionModel.scope_type == "user",
                PluginAgentSessionModel.canonical_owner_person_id == person_id,
            )
        )
        await session.execute(
            delete(PluginAgentMessageModel).where(
                PluginAgentMessageModel.canonical_sender_person_id == person_id,
            )
        )

    @staticmethod
    async def _forget_person_automations(session: AsyncSession, person_id: str) -> None:
        """Delete automations this Person created or is the send target of."""

        await session.execute(
            delete(AutomationModel).where(
                or_(
                    AutomationModel.canonical_target_person_id == person_id,
                    AutomationModel.canonical_creator_person_id == person_id,
                )
            )
        )

    @staticmethod
    async def _forget_person_memory(session: AsyncSession, person_id: str) -> None:
        """Delete Person-owned live Memory rows before canonical Person delete.

        Clusters first (RESTRICT on persons). Facts next so their evidence
        CASCADE stays on this Person. Jobs/receipts/reflection after that, so
        another Person's evidence is not removed via receipt CASCADE. Dream
        run ledgers stay as provenance.
        """

        person_dream = or_(
            MemoryDreamClusterModel.canonical_subject_person_id == person_id,
            MemoryDreamClusterModel.canonical_visibility_person_id == person_id,
        )
        await session.execute(delete(MemoryDreamClusterModel).where(person_dream))
        await session.execute(
            delete(MemoryFactModel).where(
                or_(
                    MemoryFactModel.canonical_subject_person_id == person_id,
                    MemoryFactModel.canonical_visibility_person_id == person_id,
                )
            )
        )
        await session.execute(
            delete(MemoryJobModel).where(MemoryJobModel.canonical_person_id == person_id)
        )
        await session.execute(
            delete(MemoryToolReceiptModel).where(
                MemoryToolReceiptModel.canonical_person_id == person_id
            )
        )
        await session.execute(
            delete(MemorySelfReflectionStateModel).where(
                MemorySelfReflectionStateModel.canonical_person_id == person_id
            )
        )
        await session.execute(
            delete(MemorySelfReflectionRunModel).where(
                MemorySelfReflectionRunModel.canonical_person_id == person_id
            )
        )

    @staticmethod
    def _bot_id_from_private_alias(scope_key: str) -> str | None:
        if not scope_key.startswith("bot:") or ":private:" not in scope_key:
            return None
        bot, separator, _peer = scope_key.removeprefix("bot:").partition(":private:")
        if not separator or not bot:
            return None
        return bot

    @staticmethod
    async def _private_conversation_alias_keys(
        session: AsyncSession,
        conversation_ids: tuple[str, ...],
        owner_externals: tuple[str, ...],
    ) -> tuple[str, ...]:
        keys: set[str] = set()
        if conversation_ids:
            for key in await session.scalars(
                select(ConversationLegacyAliasModel.scope_key).where(
                    ConversationLegacyAliasModel.conversation_id.in_(conversation_ids)
                )
            ):
                keys.add(str(key))
        bots = {
            str(item) for item in await session.scalars(select(PresenceModel.external_account_id))
        }
        for key in tuple(keys):
            bot = PeopleRepository._bot_id_from_private_alias(key)
            if bot:
                bots.add(bot)
        for owner in owner_externals:
            keys.add(f"private:{owner}")
            for bot in bots:
                keys.add(ConversationScope.private(bot, owner).key)
        return tuple(sorted(keys))

    @staticmethod
    async def _forget_person_conversation(
        session: AsyncSession,
        person_id: str,
        owner_externals: tuple[str, ...],
    ) -> None:
        """Delete Person-owned correlations before private Conversation delete.

        Inbound FKs to canonical_conversations (current schema/ORM):
        RESTRICT: chat_events, conversation_scopes, web_search_runs,
        tool_invocations, runtime_turn_observations,
        plugin_notification_outbox, plugin_background_turn_jobs,
        speech_generations and model_invocations.
        CASCADE: canonical rollup / job / emergency overlay.
        Deferred NO ACTION: conversation aliases (deleted later).

        Ledger retain: attributable chat_events are deleted later; leftover
        mention-only events keep redact + application SET NULL (nullable
        column). The plugin cleanup already deleted Person-owned rows;
        leftover Space/global plugin correlation
        may SET NULL (nullable) so RESTRICT does not block delete.
        """

        conversation_ids = tuple(
            str(item)
            for item in await session.scalars(
                select(CanonicalConversationModel.id).where(
                    CanonicalConversationModel.kind == "private",
                    CanonicalConversationModel.person_id == person_id,
                )
            )
        )
        alias_keys = await PeopleRepository._private_conversation_alias_keys(
            session, conversation_ids, owner_externals
        )
        plain_hashes = tuple(hash_conversation_key(key) for key in alias_keys)
        observation_match = [RuntimeTurnObservationModel.canonical_person_id == person_id]
        speech_match = []
        tool_match = []
        web_match = []
        if conversation_ids:
            observation_match.append(
                RuntimeTurnObservationModel.canonical_conversation_id.in_(conversation_ids)
            )
            speech_match.append(
                SpeechGenerationModel.canonical_conversation_id.in_(conversation_ids)
            )
            tool_match.append(ToolInvocationModel.canonical_conversation_id.in_(conversation_ids))
            web_match.append(WebSearchRunModel.canonical_conversation_id.in_(conversation_ids))
        if plain_hashes:
            observation_match.append(
                RuntimeTurnObservationModel.conversation_key_hash.in_(plain_hashes)
            )
            speech_match.append(SpeechGenerationModel.conversation_key_hash.in_(plain_hashes))
            tool_match.append(ToolInvocationModel.conversation_key_hash.in_(plain_hashes))
        if alias_keys:
            web_match.append(WebSearchRunModel.conversation_key.in_(alias_keys))
        await session.execute(delete(RuntimeTurnObservationModel).where(or_(*observation_match)))
        if speech_match:
            await session.execute(delete(SpeechGenerationModel).where(or_(*speech_match)))
        if tool_match:
            await session.execute(delete(ToolInvocationModel).where(or_(*tool_match)))
        if web_match:
            await session.execute(delete(WebSearchRunModel).where(or_(*web_match)))
        if conversation_ids:
            await session.execute(
                delete(ModelInvocationModel).where(
                    ModelInvocationModel.canonical_conversation_id.in_(conversation_ids)
                )
            )
            await session.execute(
                update(PluginNotificationOutboxModel)
                .where(
                    PluginNotificationOutboxModel.canonical_conversation_id.in_(conversation_ids)
                )
                .values(canonical_conversation_id=None)
            )
            await session.execute(
                update(PluginBackgroundTurnJobModel)
                .where(PluginBackgroundTurnJobModel.canonical_conversation_id.in_(conversation_ids))
                .values(canonical_conversation_id=None)
            )

    @staticmethod
    async def _affected_scopes_for_owners(
        session: AsyncSession,
        owner_ids: tuple[str, ...],
    ) -> tuple[ConversationScope, ...]:
        scopes: dict[str, ConversationScope] = {}
        for owner in owner_ids:
            for scope in await PeopleRepository.affected_conversation_scopes_in_session(
                session, owner
            ):
                scopes[scope.key] = scope
        return tuple(scopes[key] for key in sorted(scopes))

    @staticmethod
    async def affected_conversation_scopes_in_session(
        session: AsyncSession,
        user_id: str,
    ) -> tuple[ConversationScope, ...]:
        rows = (
            await session.execute(
                select(
                    ChatEventModel.bot_user_id,
                    ChatEventModel.scope_type,
                    ChatEventModel.group_id,
                    ChatEventModel.private_peer_user_id,
                )
                .where(
                    or_(
                        ChatEventModel.sender_user_id == user_id,
                        ChatEventModel.private_peer_user_id == user_id,
                        ChatEventModel.content.contains(user_id),
                        ChatEventModel.visual_summary.contains(user_id),
                        ChatEventModel.audio_transcript.contains(user_id),
                        ChatEventModel.segments_json.contains(user_id),
                    )
                )
                .distinct()
            )
        ).all()
        scopes: dict[str, ConversationScope] = {}
        for bot_user_id, scope_type, group_id, private_peer_user_id in rows:
            if scope_type == ScopeType.GROUP.value and group_id is not None:
                scope = ConversationScope.group(str(bot_user_id), str(group_id))
            elif private_peer_user_id is not None:
                scope = ConversationScope.private(
                    str(bot_user_id),
                    str(private_peer_user_id),
                )
            else:
                continue
            scopes[scope.key] = scope
        return tuple(scopes[key] for key in sorted(scopes))


class UserProfileRepository(PeopleRepository):
    """Backward-compatible name used by identity services."""

    async def upsert(
        self,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        is_bot: bool = False,
    ) -> None:
        await self.observe(
            user_id=user_id,
            nickname=nickname,
            group_id=group_id,
            group_card=group_card,
            nickname_known=nickname_known,
            group_card_known=group_card_known,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
            is_bot=is_bot,
        )

    async def delete_user(self, user_id: str) -> bool:
        return await self.delete_person(user_id)


class GroupSettingsRepository:
    """Persist group observation and autonomous participation settings."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self,
        group_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting | None:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get(group_id, session=owned_session)
        return await load_canonical_group(session, group_id)

    async def set_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(group_id, enabled, session=owned_session)
        now = datetime.now(UTC)
        return await set_canonical_space_flags(session, group_id, enabled=enabled, now=now)

    async def set_autonomous_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting:
        """Update only the group's autonomous participation switch."""

        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_autonomous_enabled(
                    group_id,
                    enabled,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        return await set_canonical_space_flags(
            session, group_id, autonomous_enabled=enabled, now=now
        )

    async def observe(
        self,
        group_id: str,
        *,
        name: str = "",
        enabled_if_new: bool = False,
    ) -> GroupSetting:
        """Create an observed group without overwriting an existing access switch."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            return await observe_canonical_space(session, group_id, name=name, now=now)


class PrivateUserSettingsRepository:
    """Private chats are allowed unless the person's row explicitly disables them."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
    ) -> None:
        self._people = PeopleRepository(
            database,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )

    async def get(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting | None:
        return await self._people.get_enabled(user_id, session=session)

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        return await self._people.set_enabled(
            user_id,
            enabled,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
            session=session,
        )
