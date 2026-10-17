"""Repositories for people, memberships, groups, and private access."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from heapq import nsmallest
from typing import Any

from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    delete,
    false,
    func,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from yuki.client_api.db_models import ClientDeviceModel
from yuki.conversation.autonomy_db_models import (
    AutonomyBindingModel,
    InitiativeFeedbackModel,
    InitiativeRunModel,
)
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
    ConversationLegacyAliasModel,
)
from yuki.domain.audio import parse_transcripts, serialize_transcripts
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.profiles import UserProfileSnapshot
from yuki.emoji.db_models import EmojiAssetModel, EmojiUsageEventModel
from yuki.identity.canonical_repository import (
    AccountRole,
    bindings_for_person,
    external_id,
    find_identity_binding,
    find_presence,
    forget_person_by_id,
    trip,
)
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.ownership_interval import BindingOwnership
from yuki.identity.person_identity import person_family_ids
from yuki.identity.person_state import (
    load_canonical_aliases,
    load_canonical_group,
    load_canonical_group_member_name_projections,
    load_canonical_group_members_by_exact_name,
    load_canonical_members_in_group,
    load_canonical_membership_count,
    load_canonical_person_enabled,
    load_canonical_profile,
    load_canonical_profiles,
    observe_canonical_person,
    set_canonical_person_enabled,
    set_canonical_space_flags,
)
from yuki.identity.write_settings import identity_write_settings
from yuki.memory.dream.db_models import MemoryDreamClusterModel
from yuki.memory.rebuild.privacy import PreparedRebuildForget
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.persistence.database import Database
from yuki.persistence.models import (
    AdminOperationEventModel,
    AutomationModel,
    ChatEventModel,
    MembershipModel,
    MemoryClaimCandidateEvidenceModel,
    MemoryClaimCandidateModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryJobModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
    PersonAliasModel,
    RuntimeConfigOverrideModel,
    RuntimeTurnObservationModel,
    ToolInvocationModel,
    WebSearchRunModel,
)
from yuki.persistence.repository_records import (
    GroupSetting,
    PrivateUserSetting,
)
from yuki.plugin_host.db_models import (
    PluginAgentMessageModel,
    PluginAgentSessionModel,
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginConfigValueModel,
    PluginNotificationOutboxModel,
    PluginStateModel,
)
from yuki.runtime.observability import hash_conversation_key
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel
from yuki.sandbox.resource_cleanup import ResourceCleanupPreparation


@dataclass(frozen=True, slots=True)
class GroupMemberNameMatch:
    user_id: str
    nickname: str
    group_card: str
    matched_alias: str
    score: float
    exact: bool
    person_id: str | None = None

    @property
    def display_name(self) -> str:
        return self.group_card or self.nickname or self.user_id


def normalize_person_name(value: str) -> str:
    """Normalize identity labels without inferring any linguistic role."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if unicodedata.category(character)[0] in {"L", "N"}
    )


def _redact_privacy_json(value: str, pattern: re.Pattern[str], marker: str) -> str:
    """Redact parsed values without corrupting numeric or escaped JSON tokens."""
    if not value:
        return value

    def redact(item: object) -> object:
        if isinstance(item, str):
            return pattern.sub(lambda _match: marker, item)
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, dict):
            return {
                pattern.sub(lambda _match: marker, str(key)): redact(child)
                for key, child in item.items()
            }
        if isinstance(item, int) and not isinstance(item, bool) and pattern.fullmatch(str(item)):
            return marker
        return item

    return json.dumps(redact(json.loads(value)), ensure_ascii=False, separators=(",", ":"))


def _privacy_json_match(
    column: InstrumentedAttribute[str], aliases: tuple[str, ...]
) -> ColumnElement[bool]:
    """Match decoded JSON keys/values, retaining malformed matching rows for rollback."""
    decoded = func.json_tree(
        case((func.json_valid(column) == 1, column), else_="null")
    ).table_valued("key", "atom")
    return or_(
        *[column.contains(alias) for alias in aliases],
        select(decoded.c.atom)
        .where(
            or_(
                *[cast(decoded.c.atom, String).contains(alias) for alias in aliases],
                *[cast(decoded.c.key, String).contains(alias) for alias in aliases],
            )
        )
        .exists(),
    )


@dataclass(frozen=True)
class PersonForgetPreparation:
    person_id: str
    person_ids: tuple[str, ...]
    ownership: tuple[BindingOwnership, ...]
    private_ids: tuple[str, ...]
    affected_ids: tuple[str, ...]
    redactions: tuple[tuple[int, dict[str, str]], ...]
    rebuild: PreparedRebuildForget
    audit_redactions: tuple[tuple[int, dict[str, str]], ...]
    sandbox_request_ids: tuple[str, ...]
    initiative_ids: tuple[str, ...]
    reflection_checkpoint_ids: tuple[int, ...]
    resource_cleanup: ResourceCleanupPreparation
    peer_cleanup_results: tuple[dict[str, Any], ...] | None = None

    @property
    def legacy_aliases(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    item.account
                    for item in self.ownership
                    if not item.bounded and item.platform == "qq"
                }
            )
        )


def _privacy_owned_events(
    person_id: str | tuple[str, ...],
    private_ids: tuple[str, ...],
    ownership: tuple[BindingOwnership, ...],
) -> ColumnElement[bool]:
    person_ids = (person_id,) if isinstance(person_id, str) else person_id
    legacy = [
        and_(
            ChatEventModel.author_person_id.is_(None),
            or_(ChatEventModel.author_kind.is_(None), ChatEventModel.author_kind == "person"),
            _ownership_window(item),
            or_(
                ChatEventModel.sender_user_id == item.account,
                and_(
                    ChatEventModel.canonical_conversation_id.is_(None),
                    ChatEventModel.private_peer_user_id == item.account,
                ),
            ),
        )
        for item in ownership
    ]
    return func.coalesce(
        or_(
            ChatEventModel.author_person_id.in_(person_ids),
            ChatEventModel.canonical_conversation_id.in_(private_ids),
            *legacy,
        ),
        false(),
    )


def _ownership_window(item: BindingOwnership) -> ColumnElement[bool]:
    source: ColumnElement[bool] = ChatEventModel.ingress_presence_id.in_(
        select(PresenceModel.id).where(PresenceModel.platform == item.platform)
    )
    if item.platform == "qq":
        source = or_(source, ChatEventModel.ingress_presence_id.is_(None))
    conditions: list[ColumnElement[bool]] = [source]
    if item.starts_at is not None:
        conditions.append(ChatEventModel.observed_at >= item.starts_at)
    if item.ends_at is not None:
        conditions.append(ChatEventModel.observed_at < item.ends_at)
    return and_(*conditions)


def _privacy_mentions(
    person_id: str | tuple[str, ...], ownership: tuple[BindingOwnership, ...]
) -> ColumnElement[bool]:
    person_ids = (person_id,) if isinstance(person_id, str) else person_id
    conditions = [_privacy_json_match(ChatEventModel.segments_json, person_ids)]
    segments = func.json_each(
        case(
            (func.json_valid(ChatEventModel.segments_json) == 1, ChatEventModel.segments_json),
            else_="[]",
        )
    ).table_valued("value")
    for item in ownership:
        if item.platform != "qq":
            continue
        actual_mention = (
            select(segments.c.value)
            .where(
                func.json_extract(segments.c.value, "$.type") == "at",
                cast(func.json_extract(segments.c.value, "$.data.qq"), String) == item.account,
            )
            .exists()
        )
        damaged_source = and_(
            func.json_valid(ChatEventModel.segments_json) == 0,
            ChatEventModel.segments_json.contains(item.account),
        )
        conditions.append(and_(_ownership_window(item), or_(actual_mention, damaged_source)))
    return or_(false(), *conditions)


def _score_group_member_identities(
    identities: dict[str, tuple[str, str, set[str]]],
    query: str,
    *,
    limit: int,
    minimum_score: float,
) -> tuple[GroupMemberNameMatch, ...]:
    matches: list[GroupMemberNameMatch] = []
    normalized_labels: dict[str, str] = {}
    scores: dict[str, float] = {}
    for user_id, (nickname, group_card, aliases) in identities.items():
        labels = tuple(dict.fromkeys((group_card, nickname, *sorted(aliases))))
        best_alias = ""
        best_score = 0.0
        best_exact = False
        for label in labels:
            if label not in normalized_labels:
                normalized_labels[label] = normalize_person_name(label)
            candidate = normalized_labels[label]
            if not candidate:
                continue
            exact = candidate == query
            if candidate not in scores:
                if exact:
                    score = 1.0
                elif query in candidate or candidate in query:
                    score = 0.75 + 0.25 * (
                        min(len(query), len(candidate)) / max(len(query), len(candidate))
                    )
                else:
                    score = SequenceMatcher(None, query, candidate).ratio()
                scores[candidate] = score
            score = scores[candidate]
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
    return tuple(
        nsmallest(
            limit,
            matches,
            key=lambda item: (
                -item.score,
                item.display_name.casefold(),
                item.user_id,
            ),
        )
    )


async def _observe_role(
    session: AsyncSession,
    user_id: str,
    *,
    is_bot: bool,
    binding: IdentityBindingModel | None = None,
    platform: str = "qq",
) -> tuple[AccountRole, IdentityBindingModel | None]:
    settings = identity_write_settings()
    if is_bot or user_id in settings.ignored_bot_users:
        return "external_bot", None
    external = external_id(user_id)
    if external in settings.ignored_bot_users:
        return "external_bot", None
    presence = await find_presence(session, external, platform=platform)
    if binding is None:
        binding = await find_identity_binding(session, external, platform=platform)
    if presence is not None and binding is None:
        return "yuki_self", None
    return "human", binding


class PeopleRepository:
    """Keep one global person and exact per-group memberships."""

    def __init__(
        self,
        database: Database,
        *,
        memory_rebuilds: MemoryRebuildRepository | None = None,
    ) -> None:
        from yuki.memory.rebuild.repository import MemoryRebuildRepository

        self._database = database
        self._memory_rebuilds = memory_rebuilds or MemoryRebuildRepository(database)

    @property
    def database(self) -> Database:
        """Canonical storage for host-owned identity read policies."""
        return self._database

    async def affected_conversation_scopes(
        self,
        person_id: str,
    ) -> tuple[ConversationScope, ...]:
        """Resolve every conversation whose retained projection may mention a person."""

        async with self._database.sessions() as session:
            if await session.get(CanonicalPersonModel, person_id) is None:
                return ()
            person_ids = await person_family_ids(session, person_id)
            ownership = tuple(
                BindingOwnership.of(item) for item in await bindings_for_person(session, person_id)
            )
            private_ids = tuple(
                await session.scalars(
                    select(CanonicalConversationModel.id).where(
                        CanonicalConversationModel.kind == "private",
                        CanonicalConversationModel.person_id.in_(person_ids),
                    )
                )
            )
            rows = (
                await session.execute(
                    select(
                        ChatEventModel.bot_user_id,
                        ChatEventModel.scope_type,
                        ChatEventModel.group_id,
                        ChatEventModel.private_peer_user_id,
                        ChatEventModel.canonical_conversation_id,
                        ChatEventModel.ingress_presence_id,
                        PresenceModel.platform,
                        CanonicalConversationModel.thread_id,
                    )
                    .outerjoin(
                        PresenceModel, PresenceModel.id == ChatEventModel.ingress_presence_id
                    )
                    .outerjoin(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    )
                    .where(
                        or_(
                            _privacy_owned_events(person_ids, private_ids, ownership),
                            _privacy_mentions(person_ids, ownership),
                        )
                    )
                    .distinct()
                )
            ).all()
        scopes: dict[str, ConversationScope] = {}
        for (
            bot_user_id,
            scope_type,
            group_id,
            private_peer_user_id,
            conversation_id,
            presence_id,
            platform,
            thread_id,
        ) in rows:
            entry = dict(
                platform=platform or "qq",
                conversation_id=conversation_id,
                presence_id=presence_id,
                thread_id=thread_id,
            )
            if scope_type == ScopeType.GROUP.value and group_id is not None:
                scope = ConversationScope.group(str(bot_user_id), str(group_id), **entry)
            elif private_peer_user_id is not None:
                scope = ConversationScope.private(
                    str(bot_user_id),
                    str(private_peer_user_id),
                    **entry,
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
        expected_person_id: str | None = None,
        platform: str = "qq",
        space_id: str | None = None,
    ) -> None:
        """Update current values and retain historical aliases."""

        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            current_binding = None
            if expected_person_id is not None:
                current_binding = await find_identity_binding(
                    session, external_id(user_id), platform=platform
                )
                if current_binding is None or current_binding.person_id != expected_person_id:
                    raise CanonicalIdentityError("canonical_owner_mismatch")
            role, current_binding = await _observe_role(
                session, user_id, is_bot=is_bot, binding=current_binding, platform=platform
            )
            if role != "human":
                return
            if current_binding is None:
                raise CanonicalIdentityError("unclassified")
            await observe_canonical_person(
                session,
                binding=current_binding,
                nickname=nickname if nickname_known else "",
                group_id=group_id,
                group_card=group_card,
                group_name=group_name,
                nickname_known=nickname_known,
                group_card_known=group_card_known,
                now=now,
                space_id=space_id,
            )

    async def get(
        self,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        include_nickname: bool = True,
        person_id: str | None = None,
        space_id: str | None = None,
        platform: str = "qq",
    ) -> UserProfileSnapshot | None:
        async with self._database.sessions() as session:
            loaded = await load_canonical_profile(
                session,
                user_id=user_id,
                group_id=group_id,
                include_nickname=include_nickname,
                person_id=person_id,
                space_id=space_id,
                platform=platform,
            )
            if loaded is None:
                return None
            nickname, card = loaded
            identity = user_id if user_id is not None else person_id
            assert identity is not None
            return UserProfileSnapshot(
                user_id=identity,
                scope_type=ScopeType.GROUP if space_id or group_id else ScopeType.PRIVATE,
                nickname=nickname,
                group_id=group_id if user_id is not None else space_id,
                group_card=card,
            )

    async def get_many(
        self,
        user_ids: tuple[str, ...],
        *,
        group_id: str | None = None,
        person_ids: Mapping[str, str] | None = None,
        members_only: bool = False,
        session: AsyncSession | None = None,
        platform: str = "qq",
        space_id: str | None = None,
    ) -> dict[str, UserProfileSnapshot]:
        """Batch the requested people and current-group cards without per-person reads."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get_many(
                    unique_ids,
                    group_id=group_id,
                    person_ids=person_ids,
                    members_only=members_only,
                    session=owned_session,
                    platform=platform,
                    space_id=space_id,
                )
        profiles = await load_canonical_profiles(
            session,
            unique_ids,
            group_id=group_id,
            person_ids=person_ids,
            members_only=members_only,
            platform=platform,
            space_id=space_id,
        )
        return {
            item: UserProfileSnapshot(
                user_id=item,
                scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                nickname=profiles[item][0],
                group_id=group_id,
                group_card=profiles[item][1],
            )
            for item in unique_ids
            if item in profiles
        }

    async def aliases(self, person_id: str | None, *, limit: int = 20) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            return await load_canonical_aliases(session, person_id, limit=limit)

    async def membership_count(self, person_id: str | None) -> int:
        async with self._database.sessions() as session:
            return await load_canonical_membership_count(session, person_id)

    async def members_in_group(
        self,
        user_ids: tuple[str, ...],
        group_id: str,
        *,
        person_ids: Mapping[str, str] | None = None,
        session: AsyncSession | None = None,
    ) -> frozenset[str]:
        """Return only identities with a real membership in the exact group."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return frozenset()
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.members_in_group(
                    unique_ids, group_id, person_ids=person_ids, session=owned_session
                )
        return await load_canonical_members_in_group(
            session, unique_ids, group_id, person_ids=person_ids
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
        platform: str = "qq",
        space_id: str | None = None,
    ) -> tuple[GroupMemberNameMatch, ...]:
        """Return deterministic current-group identity candidates for a model-supplied name."""

        query = normalize_person_name(name)
        if not query or limit <= 0:
            return ()
        async with self._database.sessions() as session:
            projections = await load_canonical_group_member_name_projections(
                session, group_id, platform=platform, space_id=space_id
            )
            projected = {
                item.user_id: (item.nickname, item.group_card, set(item.aliases))
                for item in projections
            }
            matches = _score_group_member_identities(
                projected,
                query,
                limit=limit,
                minimum_score=minimum_score,
            )
            people = {item.user_id: item.person_id for item in projections}
            return tuple(replace(item, person_id=people[item.user_id]) for item in matches)

    async def find_group_members_by_normalized_names(
        self,
        names: tuple[str, ...],
        group_id: str,
        *,
        platform: str = "qq",
        space_id: str | None = None,
    ) -> dict[str, tuple[GroupMemberNameMatch, ...]]:
        """Resolve all declared normalized exact names from one complete group projection."""
        queries = {name: normalize_person_name(name) for name in dict.fromkeys(names)}
        if not queries:
            return {}
        async with self._database.sessions() as session:
            projections = await load_canonical_group_member_name_projections(
                session, group_id, platform=platform, space_id=space_id
            )
        wanted = {query for query in queries.values() if query}
        labels: dict[str, str] = {}
        matches: dict[str, dict[str, GroupMemberNameMatch]] = {}
        for item in projections:
            for label in dict.fromkeys((item.group_card, item.nickname, *sorted(item.aliases))):
                if label not in labels:
                    labels[label] = normalize_person_name(label)
                normalized = labels[label]
                if normalized not in wanted:
                    continue
                candidates = matches.setdefault(normalized, {})
                previous = candidates.get(item.user_id)
                if previous is None or label.casefold() < previous.matched_alias.casefold():
                    candidates[item.user_id] = GroupMemberNameMatch(
                        user_id=item.user_id,
                        nickname=item.nickname,
                        group_card=item.group_card,
                        matched_alias=label,
                        score=1.0,
                        exact=True,
                        person_id=item.person_id,
                    )
        ranked = {
            query: tuple(
                sorted(
                    candidates.values(),
                    key=lambda item: (item.display_name.casefold(), item.user_id),
                )
            )
            for query, candidates in matches.items()
        }
        return {name: ranked.get(query, ()) for name, query in queries.items()}

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(
                    user_id,
                    enabled,
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
        """Resolve the authenticated current account once, then erase by Person ID."""
        async with self._database.sessions() as reader:
            binding = await find_identity_binding(reader, external_id(user_id))
            if binding is None:
                return False
            person_id = binding.person_id
        return await self.delete_person_id(person_id, marker=marker)

    async def delete_person_id(
        self,
        person_id: str,
        *,
        marker: str = "[已删除用户]",
        operation_id: str | None = None,
    ) -> bool:
        external = await self.prepare_person_forget_resources(
            person_id, operation_id=operation_id or f"person-forget:{person_id}", marker=marker
        )

        async def erase(session: AsyncSession) -> bool:
            prepared = await self.prepare_person_forget(
                person_id, marker=marker, session=session, closed_resources=external
            )
            if prepared is None:
                return False
            return await self.apply_person_forget(session, prepared, marker=marker)

        return await self._database.apply_snapshot_write(erase)

    async def prepare_person_forget_resources(
        self, person_id: str, *, operation_id: str, marker: str = "[已删除用户]"
    ) -> PersonForgetPreparation | None:
        client = self._database.sandbox_client
        if client is None:
            return None
        async with self._database.sessions() as reader:
            prepared = await self.prepare_person_forget(person_id, session=reader, marker=marker)
        if prepared is None or not prepared.resource_cleanup.has_peers:
            return None
        receipts = await client.close_peers_for_requests(
            prepared.sandbox_request_ids, operation_id, purge=True
        )
        return replace(prepared, peer_cleanup_results=tuple(receipts))

    async def prepare_person_forget(
        self,
        person_id: str,
        *,
        session: AsyncSession,
        marker: str = "[已删除用户]",
        closed_resources: PersonForgetPreparation | None = None,
    ) -> PersonForgetPreparation | None:
        from yuki.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=False) as reader:
            if await reader.get(CanonicalPersonModel, person_id) is None:
                return None
            person_ids = await person_family_ids(reader, person_id)
            ownership = tuple(
                BindingOwnership.of(item)
                for item in sorted(
                    await reader.scalars(
                        select(IdentityBindingModel).where(
                            IdentityBindingModel.person_id.in_(person_ids)
                        )
                    ),
                    key=lambda item: item.id,
                )
            )
            private_ids = tuple(
                await reader.scalars(
                    select(CanonicalConversationModel.id)
                    .where(
                        CanonicalConversationModel.kind == "private",
                        CanonicalConversationModel.person_id.in_(person_ids),
                    )
                    .order_by(CanonicalConversationModel.id)
                )
            )
            legacy = tuple(
                sorted(
                    {
                        item.account
                        for item in ownership
                        if not item.bounded and item.platform == "qq"
                    }
                )
            )
            owned = _privacy_owned_events(person_ids, private_ids, ownership)
            mentions = _privacy_mentions(person_ids, ownership)
            affected = set(private_ids)
            affected.update(
                item
                for item in await reader.scalars(
                    select(ChatEventModel.canonical_conversation_id)
                    .where(
                        or_(
                            owned,
                            _privacy_mentions(
                                person_ids, tuple(item for item in ownership if not item.bounded)
                            ),
                        )
                    )
                    .distinct()
                )
                if item is not None
            )
            redactions = []
            for event in await reader.scalars(select(ChatEventModel).where(mentions, ~owned)):
                presence = (
                    await reader.get(PresenceModel, event.ingress_presence_id)
                    if event.ingress_presence_id
                    else None
                )
                platform = presence.platform if presence is not None else "qq"
                aliases = {
                    *person_ids,
                    *(
                        item.account
                        for item in ownership
                        if item.platform == platform and item.contains(event.observed_at)
                    ),
                }
                pattern = re.compile(
                    r"(?<![A-Za-z0-9_-])(?:"
                    + "|".join(re.escape(item) for item in sorted(aliases, key=len, reverse=True))
                    + r")(?![A-Za-z0-9_-])"
                )
                redactions.append(
                    (
                        event.id,
                        dict(
                            content=pattern.sub(lambda _: marker, event.content),
                            visual_summary=pattern.sub(lambda _: marker, event.visual_summary),
                            audio_transcript=serialize_transcripts(
                                tuple(
                                    replace(item, text=pattern.sub(lambda _: marker, item.text))
                                    for item in parse_transcripts(event.audio_transcript)
                                )
                            ),
                            segments_json=_redact_privacy_json(
                                event.segments_json, pattern, marker
                            ),
                        ),
                    )
                )
            audit_redactions = []
            audit_aliases = (*person_ids, *legacy)
            pattern = re.compile(
                r"(?<![A-Za-z0-9_-])(?:"
                + "|".join(re.escape(item) for item in sorted(audit_aliases, key=len, reverse=True))
                + r")(?![A-Za-z0-9_-])"
            )
            audit_match = or_(
                AdminOperationEventModel.actor_user_id.in_(audit_aliases),
                AdminOperationEventModel.target_id.in_(audit_aliases),
                *[
                    AdminOperationEventModel.conversation_key.contains(item)
                    for item in audit_aliases
                ],
                _privacy_json_match(AdminOperationEventModel.before_json, audit_aliases),
                _privacy_json_match(AdminOperationEventModel.after_json, audit_aliases),
            )
            for audit in await reader.scalars(select(AdminOperationEventModel).where(audit_match)):
                audit_redactions.append(
                    (
                        audit.id,
                        dict(
                            actor_user_id=marker
                            if audit.actor_user_id in audit_aliases
                            else audit.actor_user_id,
                            target_id=marker
                            if audit.target_id in audit_aliases
                            else audit.target_id,
                            conversation_key=pattern.sub(lambda _: marker, audit.conversation_key),
                            before_json=_redact_privacy_json(audit.before_json, pattern, marker),
                            after_json=_redact_privacy_json(audit.after_json, pattern, marker),
                        ),
                    )
                )
            resources = await ResourceCleanupPreparation.prepare(
                reader, conversation_ids=tuple(sorted(affected))
            )
            sources = func.json_each(InitiativeRunModel.sources_json).table_valued("value")
            owned_events = select(ChatEventModel.id).where(owned)
            owned_facts = select(MemoryFactModel.id).where(
                or_(
                    MemoryFactModel.canonical_subject_person_id.in_(person_ids),
                    MemoryFactModel.canonical_visibility_person_id.in_(person_ids),
                )
            )
            source_erased = (
                select(sources.c.value)
                .where(
                    or_(
                        and_(
                            func.json_extract(sources.c.value, "$.kind") == "event",
                            cast(func.json_extract(sources.c.value, "$.source_id"), Integer).in_(
                                owned_events
                            ),
                        ),
                        and_(
                            func.json_extract(sources.c.value, "$.kind") == "memory",
                            cast(func.json_extract(sources.c.value, "$.source_id"), Integer).in_(
                                owned_facts
                            ),
                        ),
                    )
                )
                .exists()
            )
            support = func.json_each(InitiativeRunModel.support_refs_json).table_valued("value")
            support_erased = (
                select(support.c.value)
                .where(
                    or_(
                        support.c.value.in_(
                            select("event:" + cast(ChatEventModel.id, String)).where(owned)
                        ),
                        support.c.value.in_(
                            select("memory:" + cast(MemoryFactModel.id, String)).where(
                                or_(
                                    MemoryFactModel.canonical_subject_person_id.in_(person_ids),
                                    MemoryFactModel.canonical_visibility_person_id.in_(person_ids),
                                )
                            )
                        ),
                    )
                )
                .exists()
            )
            initiative_ids = tuple(
                await reader.scalars(
                    select(InitiativeRunModel.id).where(
                        or_(
                            InitiativeRunModel.conversation_id.in_(private_ids),
                            source_erased,
                            support_erased,
                        )
                    )
                )
            )
            erased_events = select(ChatEventModel.id).where(or_(owned, mentions))
            erased_tools = select(MemoryToolReceiptModel.id).where(
                or_(
                    MemoryToolReceiptModel.canonical_person_id.in_(person_ids),
                    MemoryToolReceiptModel.trigger_event_id.in_(erased_events),
                    MemoryToolReceiptModel.initiative_run_id.in_(initiative_ids),
                )
            )
            affected_facts = select(MemoryFactModel.id).where(
                or_(
                    MemoryFactModel.id.in_(owned_facts),
                    MemoryFactModel.id.in_(
                        select(MemoryEvidenceModel.fact_id).where(
                            or_(
                                MemoryEvidenceModel.event_id.in_(erased_events),
                                MemoryEvidenceModel.tool_receipt_id.in_(erased_tools),
                            )
                        )
                    ),
                )
            )
            affected_candidates = select(MemoryClaimCandidateEvidenceModel.candidate_id).where(
                MemoryClaimCandidateEvidenceModel.event_id.in_(erased_events)
            )
            checkpoint = MemorySelfReflectionRunModel.checkpoint_json
            copied_sources = []
            for field, source_ids in (
                ("event_map", erased_events),
                ("tool_map", erased_tools),
                ("fact_map", affected_facts),
                ("candidate_map", affected_candidates),
            ):
                copies = func.json_each(checkpoint, f"$.{field}").table_valued("value")
                copy_erased: ColumnElement[bool] = cast(
                    func.json_extract(copies.c.value, "$.id"), Integer
                ).in_(source_ids)
                if field == "event_map":
                    copied_mentions = func.json_each(
                        copies.c.value, "$.mentioned_person_ids"
                    ).table_valued("value")
                    copy_erased = or_(
                        copy_erased,
                        func.json_extract(copies.c.value, "$.author_person_id").in_(person_ids),
                        func.json_extract(copies.c.value, "$.canonical_conversation_id").in_(
                            private_ids
                        ),
                        select(copied_mentions.c.value)
                        .where(copied_mentions.c.value.in_(person_ids))
                        .exists(),
                    )
                elif field == "tool_map":
                    copy_erased = or_(
                        copy_erased,
                        cast(func.json_extract(copies.c.value, "$.trigger_event_id"), Integer).in_(
                            erased_events
                        ),
                        func.json_extract(copies.c.value, "$.initiative_run_id").in_(
                            initiative_ids
                        ),
                    )
                elif field == "fact_map":
                    copy_erased = or_(
                        copy_erased,
                        func.json_extract(copies.c.value, "$.canonical_subject_person_id").in_(
                            person_ids
                        ),
                        func.json_extract(copies.c.value, "$.canonical_visibility_person_id").in_(
                            person_ids
                        ),
                    )
                copied_sources.append(select(copies.c.value).where(copy_erased).exists())
            reflection_checkpoint_ids = set(
                await reader.scalars(
                    select(MemorySelfReflectionRunModel.id).where(
                        checkpoint.is_not(None), or_(*copied_sources)
                    )
                )
            )
            # Old checkpoints stored rendered context without a separate event map.
            # Read only the program's original envelope, never a body/platform label.
            context = func.json_each(checkpoint, "$.payload.context_events").table_valued("value")
            erased_context_ids = set(await reader.scalars(erased_events))
            event_conversations = select(ChatEventModel.canonical_conversation_id).where(
                ChatEventModel.id.in_(erased_events)
            )
            context_owners = select(CanonicalConversationModel).where(
                CanonicalConversationModel.id.in_(event_conversations)
            )
            owners = tuple(await reader.scalars(context_owners))
            for run_id, rendered in await reader.execute(
                select(
                    MemorySelfReflectionRunModel.id,
                    func.json_extract(context.c.value, "$.rendered"),
                )
                .select_from(MemorySelfReflectionRunModel)
                .join(context, true())
                .where(
                    checkpoint.is_not(None),
                    or_(
                        MemorySelfReflectionRunModel.canonical_person_id.in_(
                            tuple(item.person_id for item in owners if item.person_id)
                        ),
                        MemorySelfReflectionRunModel.canonical_space_id.in_(
                            tuple(item.space_id for item in owners if item.space_id)
                        ),
                    ),
                )
            ):
                envelope = re.match(
                    r"^\[发送者:.{0,128}\|(?:person:[^|]+|presence:[^|]+|[a-z_]+)"
                    r"\|事件:#([1-9]\d*)(?=[|\]])",
                    rendered,
                )
                if envelope and int(envelope[1]) in erased_context_ids:
                    reflection_checkpoint_ids.add(run_id)
        rebuild = await self._memory_rebuilds.prepare_forget_people(
            legacy, ownership=ownership, person_id=person_id, session=session
        )
        prepared = PersonForgetPreparation(
            person_id=person_id,
            person_ids=person_ids,
            ownership=ownership,
            private_ids=private_ids,
            affected_ids=tuple(sorted(affected)),
            redactions=tuple(redactions),
            rebuild=rebuild,
            audit_redactions=tuple(audit_redactions),
            sandbox_request_ids=resources.request_ids,
            initiative_ids=initiative_ids,
            reflection_checkpoint_ids=tuple(sorted(reflection_checkpoint_ids)),
            resource_cleanup=resources,
        )
        if closed_resources is not None:
            if (
                prepared.person_ids,
                prepared.ownership,
                prepared.private_ids,
                prepared.affected_ids,
            ) != (
                closed_resources.person_ids,
                closed_resources.ownership,
                closed_resources.private_ids,
                closed_resources.affected_ids,
            ):
                raise ValueError("external_agent_cleanup_source_changed")
            closed_resources.resource_cleanup.verify(resources)
            prepared = replace(prepared, peer_cleanup_results=closed_resources.peer_cleanup_results)
        return prepared

    async def apply_person_forget(
        self,
        session: AsyncSession,
        prepared: PersonForgetPreparation,
        *,
        marker: str = "[已删除用户]",
    ) -> bool:
        person_id = prepared.person_id
        if await session.get(CanonicalPersonModel, person_id) is None:
            return False
        person_ids = await person_family_ids(session, person_id)
        if person_ids != prepared.person_ids:
            raise ValueError("privacy_persons_changed")
        if prepared.resource_cleanup.has_peers:
            if prepared.peer_cleanup_results is None:
                raise ValueError("external_agent_cleanup_unconfirmed")
        ownership = prepared.ownership
        legacy = prepared.legacy_aliases
        await self._memory_rebuilds.forget_people(
            legacy, prepared=prepared.rebuild, session=session
        )
        await session.execute(
            update(MemorySelfReflectionRunModel)
            .where(MemorySelfReflectionRunModel.id.in_(prepared.reflection_checkpoint_ids))
            .values(checkpoint_json=None)
        )
        owned = _privacy_owned_events(person_ids, prepared.private_ids, ownership)
        event_ids = select(ChatEventModel.id).where(
            or_(owned, _privacy_mentions(person_ids, ownership))
        )
        await session.execute(
            delete(MemoryClaimCandidateModel).where(
                MemoryClaimCandidateModel.id.in_(
                    select(MemoryClaimCandidateEvidenceModel.candidate_id).where(
                        MemoryClaimCandidateEvidenceModel.event_id.in_(event_ids)
                    )
                )
            )
        )
        await session.execute(
            delete(WebSearchRunModel).where(
                or_(
                    WebSearchRunModel.trigger_event_id.in_(event_ids),
                    *[WebSearchRunModel.conversation_key == f"private:{item}" for item in legacy],
                    *[
                        WebSearchRunModel.conversation_key.like(f"group:%:user:{item}")
                        for item in legacy
                    ],
                )
            )
        )
        from yuki.runtime.work_repository import WorkRepository

        now = datetime.now(UTC)
        for conversation_id in prepared.affected_ids:
            await WorkRepository.purge_scope(session, conversation_id)
            if conversation_id not in prepared.private_ids:
                conversation = await session.get(CanonicalConversationModel, conversation_id)
                if conversation is not None:
                    conversation.generation += 1
                    conversation.starts_after_event_id = conversation.last_event_id
                    conversation.last_generation_change_event_id = 0
                    conversation.revision += 1
                    conversation.updated_at = now
        await session.execute(
            delete(SandboxTaskContinuationModel).where(
                SandboxTaskContinuationModel.request_id.in_(prepared.sandbox_request_ids)
            )
        )
        await session.execute(
            delete(SandboxTaskRunModel).where(
                SandboxTaskRunModel.request_id.in_(prepared.sandbox_request_ids)
            )
        )
        await session.execute(
            delete(MemoryToolReceiptModel).where(
                MemoryToolReceiptModel.initiative_run_id.in_(prepared.initiative_ids)
            )
        )
        await session.execute(
            delete(InitiativeFeedbackModel).where(
                InitiativeFeedbackModel.run_id.in_(prepared.initiative_ids)
            )
        )
        await session.execute(
            delete(InitiativeRunModel).where(InitiativeRunModel.id.in_(prepared.initiative_ids))
        )
        await session.execute(
            update(InitiativeRunModel)
            .where(InitiativeRunModel.target_person_id.in_(person_ids))
            .values(target_person_id=None)
        )
        await session.execute(
            delete(AutonomyBindingModel).where(
                AutonomyBindingModel.conversation_id.in_(prepared.private_ids)
            )
        )
        for member_id in person_ids:
            await self._forget_person_plugin(session, member_id, legacy)
            await self._forget_person_automations(session, member_id)
            await self._forget_person_memory(session, member_id)
            await self._forget_person_conversation(session, member_id, legacy)
        trip("after_forget_plugin")
        trip("after_forget_automations")
        trip("after_forget_memory")
        trip("after_forget_conversation")
        for event_id, values in prepared.redactions:
            await session.execute(
                update(ChatEventModel).where(ChatEventModel.id == event_id).values(**values)
            )
        await session.execute(
            delete(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user",
                RuntimeConfigOverrideModel.canonical_person_id.in_(person_ids),
            )
        )
        if legacy:
            for owner in legacy:
                await session.execute(
                    update(RuntimeConfigOverrideModel)
                    .where(RuntimeConfigOverrideModel.updated_by == owner)
                    .values(updated_by=marker)
                )
        for audit_id, values in prepared.audit_redactions:
            await session.execute(
                update(AdminOperationEventModel)
                .where(AdminOperationEventModel.id == audit_id)
                .values(**values)
            )
        await session.execute(
            delete(EmojiUsageEventModel).where(
                or_(
                    EmojiUsageEventModel.canonical_actor_person_id.in_(person_ids),
                    EmojiUsageEventModel.actor_user_id.in_(legacy),
                )
            )
        )
        await session.execute(
            update(EmojiAssetModel)
            .where(
                or_(
                    EmojiAssetModel.canonical_first_seen_person_id.in_(person_ids),
                    EmojiAssetModel.first_seen_user_id.in_(legacy),
                )
            )
            .values(canonical_first_seen_person_id=None, first_seen_user_id=None)
        )
        device_ids = select(ClientDeviceModel.id).where(ClientDeviceModel.person_id.in_(person_ids))
        audit_ids = select(AdminOperationEventModel.id).where(
            or_(
                AdminOperationEventModel.actor_user_id.in_(person_ids),
                and_(
                    AdminOperationEventModel.target_type == "person",
                    AdminOperationEventModel.target_id.in_(person_ids),
                ),
                AdminOperationEventModel.actor_principal_id.in_(device_ids),
                AdminOperationEventModel.actor_principal_id.in_(
                    tuple(item.binding_id for item in ownership)
                ),
            )
        )
        await session.execute(
            delete(ControlCommandReceiptModel).where(
                or_(
                    ControlCommandReceiptModel.audit_id.in_(audit_ids),
                    ControlCommandReceiptModel.principal_id.in_(device_ids),
                    ControlCommandReceiptModel.principal_id.in_(person_ids),
                )
            )
        )
        await session.execute(
            delete(AdminOperationEventModel).where(AdminOperationEventModel.id.in_(audit_ids))
        )
        owned_event_ids = select(ChatEventModel.id).where(owned)
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.reply_to_event_id.in_(owned_event_ids))
            .values(reply_to_event_id=None, reply_to_message_id=None)
        )
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.caused_by_event_id.in_(owned_event_ids))
            .values(caused_by_event_id=None)
        )
        await session.execute(delete(ChatEventModel).where(owned))
        await session.execute(
            delete(MembershipModel).where(MembershipModel.canonical_person_id.in_(person_ids))
        )
        await session.execute(
            delete(PersonAliasModel).where(PersonAliasModel.canonical_person_id.in_(person_ids))
        )
        await forget_person_by_id(session, person_id)
        return True

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

        for model in (PluginNotificationOutboxModel, PluginBackgroundTurnJobModel):
            owned_space_grant = (
                select(PluginBackgroundTargetGrantModel.id)
                .where(
                    PluginBackgroundTargetGrantModel.canonical_created_by_person_id == person_id,
                    PluginBackgroundTargetGrantModel.plugin_id == model.plugin_id,
                    PluginBackgroundTargetGrantModel.canonical_target_space_id
                    == model.canonical_target_space_id,
                )
                .exists()
            )
            await session.execute(
                delete(model).where(
                    or_(model.canonical_target_person_id == person_id, owned_space_grant)
                )
            )
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
        model_invocations.
        CASCADE: canonical Conversation projections.
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
        tool_match = []
        web_match = []
        if conversation_ids:
            observation_match.append(
                RuntimeTurnObservationModel.canonical_conversation_id.in_(conversation_ids)
            )
            tool_match.append(ToolInvocationModel.canonical_conversation_id.in_(conversation_ids))
            web_match.append(WebSearchRunModel.canonical_conversation_id.in_(conversation_ids))
        if plain_hashes:
            observation_match.append(
                RuntimeTurnObservationModel.conversation_key_hash.in_(plain_hashes)
            )
            tool_match.append(ToolInvocationModel.conversation_key_hash.in_(plain_hashes))
        if alias_keys:
            web_match.append(WebSearchRunModel.conversation_key.in_(alias_keys))
        await session.execute(delete(RuntimeTurnObservationModel).where(or_(*observation_match)))
        # Prompts and tool results can mention a Person in any conversation.
        # Disposable diagnostics must not retain content after privacy deletion.
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from yuki.execution_trace.db_models import (
            ExecutionTraceEntryModel,
            ExecutionTraceStateModel,
        )

        await session.execute(
            sqlite_insert(ExecutionTraceStateModel)
            .values(id=1, privacy_generation=1)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"privacy_generation": ExecutionTraceStateModel.privacy_generation + 1},
            )
        )
        await session.execute(delete(ExecutionTraceEntryModel))
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


class PrivateUserSettingsRepository:
    """Private chats are allowed unless the person's row explicitly disables them."""

    def __init__(
        self,
        database: Database,
    ) -> None:
        self._people = PeopleRepository(
            database,
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
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        return await self._people.set_enabled(
            user_id,
            enabled,
            session=session,
        )
