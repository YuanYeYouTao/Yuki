"""Canonical social operations shared by chat and delegated automation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select, text, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.config_service import RuntimeConfigService
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
)
from yuki.domain.conversations import ConversationScope
from yuki.execution_trace.phases import model_detail
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.gateway.social import (
    TransportMedia,
    TransportMessage,
    TransportTarget,
    prepared_transport,
    transport_payload,
)
from yuki.identity.db_models import (
    AccountAssignmentModel,
    AccountModel,
    AccountSpaceProfileModel,
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import (
    current_person_enabled_condition,
    current_person_id,
    person_enabled_condition,
    person_family_condition,
    person_root_id,
    same_person_id,
)
from yuki.identity.routing import PresenceRouter, ResolvedSend
from yuki.llm.base import LLMEmptyResponseError
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel, PersonAliasModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.persistence.unit_of_work import optional_session
from yuki.services.message_splitter import OutboundMessageSplitter
from yuki.services.renderer import sanitize_model_output
from yuki.social.content import prepare_for_entry
from yuki.social.models import (
    OperationStatus,
    SocialError,
    SocialMessage,
    SocialReceipt,
    SocialTarget,
)
from yuki.social.repository import (
    SocialOperationRepository,
    payload_hash,
    sequence_prefix,
)
from yuki.workspace.store import WorkspaceStore

if TYPE_CHECKING:
    from yuki.emoji.delivery import EmojiDeliveryService
    from yuki.workspace.service import WorkspaceService


@dataclass(frozen=True)
class SocialContext:
    turn_id: str
    call_id: str
    conversation_id: str
    person_refs: dict[str, str] = field(default_factory=dict)
    space_id: str | None = None
    # Trusted internal ledger key for an immediate reply. Platform IDs are only
    # transport references and must not reconstruct an already known event.
    trigger_event_id: int | None = None
    origin: str = "social_tool"
    caused_by_event_id: int | None = None
    actor: Any = None
    runtime_snapshot: Any = None
    turn_token: Any = None
    conversation_key: str = ""
    inbound: Any = None
    # Backend-only proof from the current private inbound event, never tool arguments.
    reply_message_id: str | None = None
    reply_presence_id: str | None = None
    sequence_part_index: int | None = None
    initiative_run_id: str | None = None
    automation_run_id: int | None = None
    presence_id: str | None = None
    # Host-owned Social parent; split parts and captions cannot replace the
    # Work effect's original domain reference with one of their own receipts.
    parent_operation_id: str | None = None
    deferred_emoji_id: str | None = None
    media_metadata: dict[str, Any] | None = None
    target_presence_id: str | None = None
    prepared_content: dict[str, Any] | None = None


def _self_scene(context: SocialContext) -> bool:
    return context.initiative_run_id is not None or context.automation_run_id is not None


def _mentioned_accounts(messages: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Project confirmed native targets from the original persisted transport plan."""
    accounts: dict[str, dict[str, str]] = {}
    for message in messages:
        for mention in message["mentions"]:
            account_id = mention.get("account_id")
            if account_id is not None:
                accounts.setdefault(
                    account_id,
                    {
                        "account_id": account_id,
                        "display_name": mention["display_name"],
                        "reference": f"@{{account:{account_id}}}",
                    },
                )
    return list(accounts.values())


def _attachment_snapshot(segments: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            {**item["data"], "attachment_kind": item["type"]}
            for item in segments
            if item["type"] in {"file", "image"} and item["data"].get("artifact_id")
        ),
        None,
    )


def _prepared_parts(prepared: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Read the original physical plan, including pre-vector direct receipts."""
    return tuple(prepared["parts"]) if "parts" in prepared else (prepared["message"],)


class SocialService:
    def __init__(
        self, database: Database, router: PresenceRouter, writer: ScopedEventLedgerUnitOfWork
    ) -> None:
        self.database = database
        self.router = router
        self.writer = writer
        self.receipts = SocialOperationRepository(database)
        self.runtime_config: RuntimeConfigService | None = None
        self.workspace: WorkspaceStore | None = None
        self.workspace_service: WorkspaceService | None = None
        self.emoji_delivery: EmojiDeliveryService | None = None

    async def _validate_person_context(
        self, context: SocialContext, *, session: AsyncSession | None = None
    ) -> None:
        actor_id = getattr(context.actor, "person_id", None)
        if actor_id is not None:
            async with optional_session(self.database, session, write=False) as reader:
                if not await reader.scalar(select(person_enabled_condition(actor_id))):
                    raise SocialError("permission_denied")

    async def _validate_self_context(
        self, context: SocialContext, *, session: AsyncSession | None = None
    ) -> None:
        if context.automation_run_id is not None:
            if (
                not context.space_id
                or not context.presence_id
                or context.trigger_event_id is not None
                or context.caused_by_event_id is not None
                or context.inbound is not None
                or context.actor is None
                or context.actor.principal_kind != "self"
                or context.actor.automation_run_id != context.automation_run_id
                or context.actor.conversation_id != context.conversation_id
                or context.actor.presence_id != context.presence_id
            ):
                raise SocialError("invalid_self_context")
            from yuki.automation.authority import DelegatedAuthority, PermissionLevel
            from yuki.automation.repository import AutomationRepository

            async with optional_session(self.database, session, write=False) as session:
                owner = await AutomationRepository(self.database).get_for_run(
                    context.automation_run_id, session=session
                )
                conversation = await session.get(
                    CanonicalConversationModel, context.conversation_id
                )
                if (
                    owner is None
                    or conversation is None
                    or owner.creator_kind != "self"
                    or owner.creator_user_id
                    or owner.canonical_creator_person_id is not None
                    or owner.canonical_target_person_id is not None
                    or owner.canonical_target_space_id != context.space_id
                    or owner.canonical_presence_id != context.presence_id
                    or conversation.kind != "space"
                    or conversation.space_id != context.space_id
                ):
                    raise SocialError("self_automation_scene_changed")
                try:
                    authority = DelegatedAuthority.model_validate(owner.authority_snapshot)
                except ValueError:
                    raise SocialError("self_automation_scene_changed") from None
                if (
                    authority.principal_kind != "self"
                    or authority.permission_level is not PermissionLevel.SELF
                    or authority.bot_user_id != owner.bot_user_id
                    or authority.bot_user_id != context.actor.bot_user_id
                    or authority.current_group_id != context.actor.group_id
                    or authority.canonical_conversation_id != context.conversation_id
                    or authority.canonical_space_id != context.space_id
                    or authority.canonical_presence_id != context.presence_id
                ):
                    raise SocialError("self_automation_scene_changed")
            return
        if context.initiative_run_id is None and (
            context.actor is None or context.actor.principal_kind != "self"
        ):
            return
        if (
            not context.initiative_run_id
            or not context.space_id
            or not context.presence_id
            or context.trigger_event_id is not None
            or context.caused_by_event_id is not None
            or context.inbound is not None
            or context.actor is None
            or context.actor.principal_kind != "self"
            or context.actor.initiative_run_id != context.initiative_run_id
            or context.actor.conversation_id != context.conversation_id
            or context.actor.presence_id != context.presence_id
        ):
            raise SocialError("invalid_self_context")
        from yuki.conversation.self_initiative import validate_self_initiative

        try:
            await validate_self_initiative(
                self.database,
                context.initiative_run_id,
                conversation_id=context.conversation_id,
                space_id=context.space_id,
                presence_id=context.presence_id,
                session=session,
            )
        except PermissionError as exc:
            raise SocialError(str(exc)) from exc

    @staticmethod
    def _message_arguments(args: dict[str, Any]) -> dict[str, Any]:
        """Validate only the current fixed send contract."""
        outer_fields = {"target", "reply_to_event_id"}
        if set(args) - (SocialMessage.model_fields.keys() | outer_fields):
            raise SocialError("invalid_message_arguments")
        return {key: value for key, value in args.items() if key in SocialMessage.model_fields}

    @staticmethod
    def _canonical_send_arguments(args: dict[str, Any]) -> tuple[dict[str, Any], SocialMessage]:
        """Keep the normalized payload and its validated message for this send."""

        message_arguments = SocialService._message_arguments(args)
        canonical = dict(args)
        raw_text = canonical.get("text", "")
        if isinstance(raw_text, str):
            try:
                canonical["text"] = sanitize_model_output(raw_text)
            except LLMEmptyResponseError:
                canonical["text"] = ""
        if isinstance(canonical.get("text"), str) and canonical["text"].strip() == "NO_REPLY":
            raise SocialError("control_token_not_message")
        if not canonical.get("text") and not any(
            canonical.get(key) for key in ("artifact_id", "path", "emoji")
        ):
            raise SocialError("empty_message_after_sanitization")
        message_arguments["text"] = canonical.get("text", "")
        return canonical, SocialMessage.model_validate(message_arguments)

    async def _path_artifact(
        self,
        message: SocialMessage,
        args: dict[str, Any],
        context: SocialContext,
        target: SocialTarget,
        prior: SocialReceipt | None,
    ) -> dict[str, Any]:
        if prior is not None and prior.prepared is not None:
            frozen = prior.prepared.get("artifact")
            if frozen is not None:
                return cast(dict[str, Any], frozen)
        if self.workspace_service is None:
            raise SocialError("artifact_transport_unavailable")
        from yuki.workspace.store import WorkspaceError

        identity = str(
            uuid5(
                NAMESPACE_URL,
                json.dumps(
                    ["yuki:send", context.conversation_id, context.turn_id, context.call_id],
                    separators=(",", ":"),
                ),
            )
        )
        try:
            metadata = await self.workspace_service.publish_for_send(
                path=cast(str, message.path),
                request_id=f"send-publish:{identity}",
                artifact_id=identity,
                source_key="send:"
                + payload_hash("send_message", target, args, context.conversation_id),
                name=message.name,
                expected_version=message.expected_version,
            )
        except WorkspaceError as exc:
            raise SocialError(str(exc)) from exc
        return metadata

    async def _binding_choice(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        selected = args.get("target")
        if (
            not isinstance(selected, dict)
            or selected.get("kind") != "person"
            or selected.get("binding_id")
            or selected.get("conversation_id")
        ):
            return None
        async with self.database.read_session() as session:
            platform = await session.scalar(
                select(AccountModel.platform)
                .select_from(IdentityBindingModel)
                .join(AccountModel, AccountModel.id == IdentityBindingModel.account_id)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.identity_binding_id == IdentityBindingModel.id,
                )
                .where(CanonicalConversationModel.id == context.conversation_id)
            )
        if platform not in LOCAL_CLIENT_PLATFORMS:
            return None
        target = await self.target("person", selected, context)
        await self.check_target(target)
        async with self.database.read_session() as session:
            bindings = list(
                await session.scalars(
                    select(IdentityBindingModel)
                    .where(
                        person_family_condition(IdentityBindingModel.person_id, str(target.id)),
                        IdentityBindingModel.status == "active",
                    )
                    .order_by(IdentityBindingModel.id)
                )
            )
        return {
            "error": "binding_required",
            "target": target.model_dump(mode="json"),
            "bindings": [
                {"binding_id": row.id, "platform": row.platform, "display_name": row.display_name}
                for row in bindings
            ],
        }

    async def contacts(
        self, kind: str, query: str = "", *, exact: bool = False
    ) -> list[dict[str, str]]:
        async with self.database.read_session() as session:
            if kind == "person":
                known = (
                    select(ChatEventModel.id)
                    .where(
                        person_family_condition(
                            ChatEventModel.author_person_id, CanonicalPersonModel.id
                        ),
                        ChatEventModel.direction == "inbound",
                        ChatEventModel.author_kind == "person",
                    )
                    .exists()
                )
                name_matches = (
                    CanonicalPersonModel.display_name == query
                    if exact
                    else CanonicalPersonModel.display_name.contains(query, autoescape=True)
                )
                alias_matches = (
                    select(PersonAliasModel.id)
                    .where(
                        person_family_condition(
                            PersonAliasModel.canonical_person_id, CanonicalPersonModel.id
                        ),
                        PersonAliasModel.canonical_space_id.is_(None),
                        PersonAliasModel.alias == query
                        if exact
                        else PersonAliasModel.alias.contains(query, autoescape=True),
                    )
                    .exists()
                )
                candidates = (
                    select(
                        CanonicalPersonModel.id,
                        CanonicalPersonModel.display_name,
                    )
                    .where(
                        CanonicalPersonModel.enabled.is_(True),
                        CanonicalPersonModel.merged_into_person_id.is_(None),
                        known,
                        or_(name_matches, alias_matches) if query else true(),
                    )
                    .order_by(CanonicalPersonModel.id)
                )
                if not exact:
                    candidates = candidates.limit(20)
                rows = (await session.execute(candidates)).all()
            elif kind == "space":
                statement = (
                    select(CanonicalSpaceModel.id, CanonicalSpaceModel.name)
                    .where(
                        CanonicalSpaceModel.enabled.is_(True),
                        CanonicalSpaceModel.name == query
                        if exact
                        else CanonicalSpaceModel.name.contains(query, autoescape=True),
                    )
                    .order_by(CanonicalSpaceModel.id)
                )
                if not exact:
                    statement = statement.limit(20)
                rows = (await session.execute(statement)).all()
            else:
                raise SocialError("invalid_target_kind")
        return [
            {"target_id": str(row[0]), "display_name": str(row[1]), "kind": kind} for row in rows
        ]

    async def target(
        self, kind: str, args: dict[str, Any], context: SocialContext, *, validate: bool = True
    ) -> SocialTarget:
        selectors = [key for key in ("target_id", "display_name", "subject_ref") if args.get(key)]
        if not selectors and kind == "space" and context.space_id:
            raw = context.space_id
        elif not selectors and kind == "person" and context.person_refs.get("current_speaker"):
            raw = context.person_refs["current_speaker"]
        elif len(selectors) != 1:
            raise SocialError("target_selector_conflict")
        elif selectors[0] == "subject_ref":
            if kind != "person":
                raise SocialError("group_target_required")
            if args["subject_ref"] not in context.person_refs:
                raise SocialError("subject_ref_unavailable")
            raw = context.person_refs[args["subject_ref"]]
        elif selectors[0] == "display_name":
            matches = [
                row for row in await self.contacts(kind, str(args["display_name"]), exact=True)
            ]
            if len(matches) != 1:
                raise SocialError("target_ambiguous" if matches else "target_not_found")
            raw = matches[0]["target_id"]
        else:
            raw = str(args["target_id"])
        target = SocialTarget.model_validate({"kind": kind, "id": raw})
        if kind == "person":
            async with self.database.read_session() as session:
                raw = await current_person_id(session, str(target.id))
            target = target.model_copy(update={"id": UUID(raw)})
        if validate:
            await self.check_target(target)
        return target

    async def check_target(
        self,
        target: SocialTarget,
        *,
        session: AsyncSession | None = None,
        known_event_id: int | None = None,
    ) -> int | None:
        async with optional_session(self.database, session, write=False) as session:
            if target.kind == "person":
                enabled = await session.scalar(
                    select(current_person_enabled_condition(str(target.id)))
                )
                known_query = select(ChatEventModel.id).where(
                    person_family_condition(ChatEventModel.author_person_id, str(target.id)),
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.author_kind == "person",
                )
                if known_event_id is not None:
                    known_query = known_query.where(ChatEventModel.id == known_event_id)
                known = await session.scalar(known_query.limit(1))
                if not enabled or known is None:
                    raise SocialError("contact_not_allowed")
                return int(known)
            else:
                space = await session.get(CanonicalSpaceModel, str(target.id))
                if space is None or not space.enabled:
                    raise SocialError("space_not_allowed")
                return None

    async def _group_directory(
        self, target: SocialTarget, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any]:
        async with self.database.read_session() as session:
            binding_id = args.get("binding_id")
            conversation_id = args.get("conversation_id") or context.conversation_id
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if (
                binding_id is None
                and conversation is not None
                and conversation.space_id == str(target.id)
            ):
                binding_id = conversation.space_binding_id
            statement = select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == str(target.id)
            )
            if binding_id is not None:
                statement = statement.where(SpaceBindingModel.id == binding_id)
            bindings = list(await session.scalars(statement.order_by(SpaceBindingModel.id)))
            if len(bindings) != 1:
                return {
                    "error": "space_entry_ambiguous" if bindings else "space_entry_unavailable",
                    "bindings": [
                        {
                            "binding_id": row.id,
                            "platform": row.platform,
                            "display_name": row.display_name,
                        }
                        for row in bindings
                    ],
                }
            binding = bindings[0]
            cursor = args.get("cursor")
            if cursor:
                try:
                    cursor = str(UUID(cursor))
                except ValueError as exc:
                    raise SocialError("invalid_cursor") from exc
            limit = int(args.get("limit", 20))
            query = (
                select(AccountModel, AccountSpaceProfileModel.display_name)
                .join(
                    AccountSpaceProfileModel, AccountSpaceProfileModel.account_id == AccountModel.id
                )
                .where(AccountSpaceProfileModel.space_binding_id == binding.id)
                .order_by(AccountSpaceProfileModel.account_id)
                .limit(limit + 1)
            )
            if cursor:
                query = query.where(AccountSpaceProfileModel.account_id > cursor)
            rows = (await session.execute(query)).all()
            page = rows[:limit]
            account_ids = [account.id for account, _ in page]
            assignments = (
                {
                    row.account_id: row
                    for row in await session.scalars(
                        select(AccountAssignmentModel).where(
                            AccountAssignmentModel.account_id.in_(account_ids),
                            AccountAssignmentModel.status != "ended",
                        )
                    )
                }
                if account_ids
                else {}
            )
            items = []
            for account, card in page:
                assignment = assignments.get(account.id)
                items.append(
                    {
                        "account_id": account.id,
                        "reference": f"@{{account:{account.id}}}",
                        "display_name": card or account.display_name or "未识别成员",
                        "assignment_id": assignment.id if assignment else None,
                        "person_id": assignment.person_id if assignment else None,
                        "presence_id": assignment.id
                        if assignment and assignment.kind == "yuki"
                        else None,
                    }
                )
            return {
                "items": items,
                "coverage": "observed",
                "binding_id": binding.id,
                "next_cursor": page[-1][0].id if len(rows) > limit else None,
            }

    @staticmethod
    def _route_payload(route: ResolvedSend) -> dict[str, Any]:
        return {
            "presence_id": route.presence_id,
            "binding_id": route.binding_id,
            "conversation_id": route.conversation_id,
            "platform": route.platform,
            "kind": route.kind,
        }

    async def _stored_route(self, target: SocialTarget, route: dict[str, Any]) -> ResolvedSend:
        if route["conversation_id"]:
            resolved = await self.router.resolve_send_for_conversation(
                route["conversation_id"], presence_id=route["presence_id"]
            )
            return replace(resolved, kind=route["kind"])
        if target.kind == "person":
            return await self.router.resolve_send_for_binding(
                str(target.id), route["binding_id"], preferred_presence_id=route["presence_id"]
            )
        return await self.router.resolve_send_for_space_binding(
            str(target.id), route["binding_id"], preferred_presence_id=route["presence_id"]
        )

    async def route(self, target: SocialTarget) -> ResolvedSend:
        # PresenceRouter may provision a missing route; explicit social operations must not.
        async with self.database.read_session() as session:
            model = PersonActiveRouteModel if target.kind == "person" else SpaceActiveRouteModel
            route = cast(
                PersonActiveRouteModel | SpaceActiveRouteModel | None,
                await session.get(model, str(target.id)),
            )
            if route is None or route.paused:
                raise SocialError("route_paused")
        result = (
            await self.router.resolve_send_for_person(str(target.id))
            if target.kind == "person"
            else await self.router.resolve_send_for_space(str(target.id))
        )
        async with self.database.read_session() as session:
            binding_model = IdentityBindingModel if target.kind == "person" else SpaceBindingModel
            binding = cast(
                IdentityBindingModel | SpaceBindingModel | None,
                await session.get(binding_model, result.binding_id),
            )
            if binding is None or binding.status != "active":
                raise SocialError("binding_unavailable")
        return result

    async def send_route(
        self, target: SocialTarget, context: SocialContext, binding_id: str | None = None
    ) -> ResolvedSend:
        async with self.database.read_session() as session:
            source = await session.get(CanonicalConversationModel, context.conversation_id)
            event = (
                await session.get(ChatEventModel, context.trigger_event_id)
                if context.trigger_event_id
                else None
            )
            if context.trigger_event_id and (
                event is None or event.canonical_conversation_id != context.conversation_id
            ):
                raise SocialError("invalid_reply_context")
            same_entry = source is not None and (
                source.space_id == str(target.id)
                if target.kind == "space"
                else bool(
                    source.person_id
                    and await same_person_id(session, source.person_id, str(target.id))
                )
            )
        if (
            source is not None
            and same_entry
            and (binding_id is None or source.identity_binding_id == binding_id)
        ):
            return await self.router.resolve_send_for_conversation(
                source.id,
                source_event_id=context.trigger_event_id or context.caused_by_event_id,
                presence_id=context.target_presence_id
                or context.reply_presence_id
                or context.presence_id
                or (event.ingress_presence_id if event is not None else None),
            )
        if binding_id is not None:
            if target.kind != "person":
                raise SocialError("binding_requires_person")
            return await self.router.resolve_send_for_binding(
                str(target.id), binding_id, preferred_presence_id=context.target_presence_id
            )
        return await self.route(target)

    async def _validate_reply_route(
        self,
        session: AsyncSession,
        context: SocialContext,
        target: SocialTarget,
        route: ResolvedSend,
    ) -> None:
        """Recheck the same internal inbound proof used during reply preparation."""
        if route.kind != "conversation" or context.trigger_event_id is None or _self_scene(context):
            return
        event = (
            await session.execute(
                select(
                    ChatEventModel.direction,
                    ChatEventModel.canonical_conversation_id,
                    ChatEventModel.ingress_presence_id,
                    ChatEventModel.scope_type,
                    ChatEventModel.sender_user_id,
                    ChatEventModel.group_id,
                    ChatEventModel.author_kind,
                    ChatEventModel.author_person_id,
                    ChatEventModel.suppression_status,
                    CanonicalConversationModel.kind.label("conversation_kind"),
                    CanonicalConversationModel.person_id,
                    CanonicalConversationModel.space_id,
                )
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                )
                .where(ChatEventModel.id == context.trigger_event_id)
            )
        ).one_or_none()
        if (
            event is None
            or event.direction != "inbound"
            or event.suppression_status != "keeper"
            or event.canonical_conversation_id != context.conversation_id
            or event.ingress_presence_id != route.presence_id
            or event.scope_type != ("private" if target.kind == "person" else "group")
            or event.conversation_kind != ("private" if target.kind == "person" else "space")
            or (target.kind == "space" and event.space_id != str(target.id))
            or (
                target.kind == "person"
                and (
                    not event.person_id
                    or not await same_person_id(session, event.person_id, str(target.id))
                )
            )
            or (
                target.kind == "person"
                and (
                    event.author_kind != "person"
                    or not event.author_person_id
                    or not await same_person_id(session, event.author_person_id, str(target.id))
                )
            )
        ):
            raise SocialError("invalid_reply_context")

    async def reply_reference(
        self, event_id: int, target: SocialTarget, route: ResolvedSend, context: SocialContext
    ) -> str:
        async with self.database.read_session() as session:
            event = await session.get(ChatEventModel, event_id)
        if (
            event is None
            or event.event_kind != "message"
            or event.canonical_conversation_id != context.conversation_id
            or (
                route.conversation_id is not None
                and event.canonical_conversation_id != route.conversation_id
            )
            or event.scope_type != ("group" if target.kind == "space" else "private")
        ):
            raise SocialError("reply_event_unavailable")
        async with self.database.read_session() as session:
            presence = await session.get(PresenceModel, event.ingress_presence_id)
        if presence is None or presence.platform != route.platform:
            raise SocialError("reply_event_unavailable")
        return event.platform_message_id

    async def _send_message_sequence(
        self,
        args: dict[str, Any],
        context: SocialContext,
        target: SocialTarget,
        route: ResolvedSend,
        message: TransportMessage,
        ledger: tuple[dict[str, Any], ...],
        prior: SocialReceipt | None,
        known_event_id: int | None,
    ) -> dict[str, Any]:
        """Plan protocol parts before dispatch; every actual send owns its receipt."""
        snapshot = context.runtime_snapshot
        if snapshot is None and self.runtime_config is not None:
            snapshot = await self.runtime_config.snapshot()
        if (
            prior is not None
            and prior.prepared
            and ("parts" in prior.prepared or "message" in prior.prepared)
        ):
            planned = tuple(
                prepared_transport(value, message.media)
                for value in _prepared_parts(prior.prepared)
            )
        else:
            layout: tuple[TransportMessage, ...] = (message,)
            if message.media is None and message.text and snapshot is not None:
                layout = OutboundMessageSplitter.render_message(message, runtime=snapshot)
            operations = self.router.operations(route)
            planned = tuple(part for item in layout for part in operations.plan_send(item))
        illustration: dict[str, Any] | None = None

        def arguments(part: TransportMessage, index: int) -> dict[str, Any]:
            value = dict(args, text=part.text)
            if part.media is None:
                for field in (
                    "artifact_id",
                    "path",
                    "name",
                    "expected_version",
                    "attachment_kind",
                    "emoji",
                ):
                    value.pop(field, None)
            if part.reply_reference is None:
                value.pop("reply_to_event_id", None)
            return value

        def segments(part: TransportMessage) -> tuple[dict[str, Any], ...]:
            result = []
            for item in ledger:
                if item["type"] == "text":
                    if part.text:
                        result.append({"type": "text", "data": {"text": part.text}})
                elif item["type"] == "mention":
                    continue
                elif item["type"] not in {"image", "file"} or part.media is not None:
                    result.append(item)
            result.extend(
                {
                    "type": "mention",
                    "data": {
                        **asdict(item),
                        "source": {
                            "platform": route.platform,
                            "external_account_id": item.external_id,
                        },
                    },
                }
                for item in part.mentions
            )
            return tuple(result)

        async def send(
            part: TransportMessage, part_context: SocialContext, payload: dict[str, Any]
        ) -> dict[str, Any]:
            nonlocal illustration
            content = part.text
            if not content and part.media is not None and part.media.kind == "file":
                content = f"[文件: {part.media.filename}]"
            elif (
                not content
                and part.media is not None
                and part_context.deferred_emoji_id is not None
            ):
                summary = next(
                    (
                        item["data"].get("summary", "表情")
                        for item in ledger
                        if item["type"] == "image"
                    ),
                    "表情",
                )
                content = f"[表情: {summary}]" if summary != "表情" else "[表情]"
            result = await self._effect(
                "send_message",
                payload,
                part_context,
                target,
                route,
                "send",
                {"message": part},
                content=content,
                ledger_segments=segments(part),
                known_event_id=known_event_id,
                emoji_request=(args, context)
                if args.get("emoji") and self.emoji_delivery is not None
                else None,
            )
            if "emoji" in result:
                illustration = result["emoji"]
            return result

        if len(planned) == 1 and (prior is None or prior.action == "send_message"):
            return await send(planned[0], context, args)
        if (
            prior is not None
            and prior.action == "send_message"
            and prior.status is not OperationStatus.PREPARED
        ):
            # An original direct receipt cannot acquire a different physical plan.
            return await self._receipt_result(prior, context)
        from yuki.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None and not (capture.work_id and capture.effect_key):
            capture = None
        part_arguments = tuple(arguments(part, index) for index, part in enumerate(planned))
        parent, dispatch = await self.receipts.prepare_sequence(
            source_turn_id=context.turn_id,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            target=target,
            payload=args,
            parts=part_arguments,
            prepared={
                "message": transport_payload(message),
                "parts": [transport_payload(part) for part in planned],
                "route": self._route_payload(route),
                "artifact": _attachment_snapshot(ledger),
            },
            work_effect=capture if context.parent_operation_id is None else None,
        )
        if dispatch:
            prefix = sequence_prefix(context.call_id)
            try:
                for index, part in enumerate(planned):
                    if index and snapshot is not None:
                        delay = random.uniform(
                            snapshot.reply.delay_min_seconds, snapshot.reply.delay_max_seconds
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                    part_context = replace(
                        context,
                        call_id=f"{prefix}{index}",
                        sequence_part_index=index,
                        parent_operation_id=parent.operation_id,
                    )
                    try:
                        result = await send(part, part_context, part_arguments[index])
                    except Exception as exc:
                        if not index:
                            raise
                        # A later preflight cannot erase a confirmed original part.
                        logging.getLogger(__name__).warning(
                            "social_sequence_preflight_failed category=%s", type(exc).__name__
                        )
                        await self.receipts.stop_sequence(parent.operation_id, type(exc).__name__)
                        if args.get("emoji") and self.emoji_delivery is not None:
                            self.emoji_delivery.wake()
                        break
                    if result.get("status") != OperationStatus.SUCCEEDED.value:
                        break
            except asyncio.CancelledError:
                # A host shutdown ends this dispatch loop, not the accepted
                # illustration. Unsent child receipts remain prepared.
                await self.receipts.stop_sequence(parent.operation_id, "CancelledError")
                if args.get("emoji") and self.emoji_delivery is not None:
                    self.emoji_delivery.wake()
                raise
        result = await self._sequence_result(parent, context)
        if illustration is not None:
            result["emoji"] = illustration
        return result

    async def _sequence_result(
        self, parent: SocialReceipt, context: SocialContext
    ) -> dict[str, Any]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            result = await self.receipts.delivery_facts(
                session,
                parent.operation_id,
                conversation_id=context.conversation_id,
                tool_call_id=context.call_id,
                planned_file_parts=None,
                origin=context.origin,
                caused_by_event_id=context.caused_by_event_id,
            )
        assert result is not None
        if parent.prepared is not None:
            accounts = _mentioned_accounts(
                parent.prepared["parts"][index]
                for index, part in enumerate(result["parts"])
                if part["status"] == OperationStatus.SUCCEEDED.value
            )
            if accounts:
                result["mentioned_accounts"] = accounts
        if result["status"] != "succeeded":
            last: dict[str, Any] = next(
                (part for part in result["parts"] if part["status"] != "succeeded"), {}
            )
            result["error"] = last.get("error_category") or (
                "delivery_uncertain" if result["status"] == "uncertain" else "delivery_failed"
            )
        return result

    async def execute(
        self, name: str, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any]:
        from yuki.social.source_keys import social_call_key

        context = replace(context, call_id=social_call_key(context.call_id))
        if name == "send":
            with model_detail("social_preparation"):
                args, message = self._canonical_send_arguments(args)
                prior = await self.receipts.find(context.turn_id, context.call_id)
                replay = await self._replay_receipt(args, context, prior)
                if replay is not None:
                    if message.emoji is not None and self.emoji_delivery is not None:
                        illustration = await self._replay_emoji(args, context)
                        if illustration is not None:
                            replay["emoji"] = illustration
                    return replay
                if message.emoji is not None and self.emoji_delivery is not None:
                    illustration = await self._replay_emoji(args, context)
                    if illustration is not None and not message.text.strip():
                        return {
                            "status": "unknown"
                            if illustration["status"] == "unknown"
                            else "accepted",
                            "emoji": illustration,
                        }
                await self._validate_person_context(context)
                choice = await self._binding_choice(args, context)
                if choice is not None:
                    return choice
        await self._validate_self_context(context)
        if name not in {"send", "query_chat_history"}:
            await self._validate_person_context(context)
        if name == "query_chat_history":
            from yuki.social.history import read_history

            return await read_history(self.database, args, context)
        if name == "find_contacts":
            kind = str(args.get("kind", "person"))
            items: list[dict[str, Any]]
            if args.get("target_id") or args.get("subject_ref"):
                target = await self.target(kind, args, context)
                items = [{"target_id": str(target.id), "kind": target.kind}]
            else:
                items = [
                    dict(item)
                    for item in await self.contacts(kind, str(args.get("display_name", "")))
                ]
            async with self.database.read_session() as session:
                selected_ids = [item["target_id"] for item in items]
                bindings_by_target: dict[str, list[dict[str, str]]] = {
                    target_id: [] for target_id in selected_ids
                }
                if selected_ids:
                    model = IdentityBindingModel if kind == "person" else SpaceBindingModel
                    owner_column = (
                        IdentityBindingModel.person_id
                        if kind == "person"
                        else SpaceBindingModel.space_id
                    )
                    bindings = (
                        await session.execute(
                            select(
                                person_root_id(owner_column) if kind == "person" else owner_column,
                                model.id,
                                model.platform,
                            )
                            .where(
                                (
                                    person_root_id(owner_column)
                                    if kind == "person"
                                    else owner_column
                                ).in_(selected_ids),
                                model.status == "active",
                            )
                            .order_by(model.id)
                        )
                    ).all()
                    for owner_id, binding_id, platform in bindings:
                        bindings_by_target[owner_id].append(
                            {
                                "binding_id": binding_id,
                                "platform": platform,
                            }
                        )
                for item in items:
                    item["bindings"] = bindings_by_target[item["target_id"]]
            return (
                items[0] if args.get("target_id") or args.get("subject_ref") else {"items": items}
            )
        if name == "recall_own_message":
            return await self._recall(args, context)
        if name == "send":
            selected = args.get("target") or {}
            if not isinstance(selected, dict):
                raise SocialError("invalid_message_arguments")
            kind = selected.get("kind") or ("space" if context.space_id else "person")
            if kind not in {"person", "space"}:
                raise SocialError("invalid_target_kind")
            if set(selected) - {"kind", "target_id", "binding_id", "conversation_id"}:
                raise SocialError("invalid_message_arguments")
            if args.get("target") and not selected.get("target_id"):
                raise SocialError("target_selector_conflict")
            target_args = selected
        elif name == "get_group_members":
            kind = "space"
            target_args = args
        else:
            raise SocialError("unknown_tool")
        with model_detail("social_preparation"):
            target = await self.target(kind, target_args, context, validate=False)
        if name == "get_group_members":
            return await self._group_directory(target, args, context)
        with model_detail("social_preparation"):
            known_event_id = await self.check_target(target)
            original_route = (
                prior.prepared.get("route") if prior is not None and prior.prepared else None
            )
            selected_entry = (args.get("target") or {}).get("conversation_id")
            if original_route is not None:
                route = await self._stored_route(target, original_route)
            elif selected_entry:
                route = await self.router.resolve_send_for_conversation(
                    selected_entry, presence_id=context.target_presence_id
                )
                if selected_entry != context.conversation_id:
                    route = replace(route, kind=target.kind)
                async with self.database.read_session() as reader:
                    selected_conversation = await reader.get(
                        CanonicalConversationModel, selected_entry
                    )
                    belongs = selected_conversation is not None and (
                        selected_conversation.space_id == str(target.id)
                        if target.kind == "space"
                        else bool(
                            selected_conversation.person_id
                            and await same_person_id(
                                reader, selected_conversation.person_id, str(target.id)
                            )
                        )
                    )
                    if not belongs:
                        raise SocialError("target_entry_mismatch")
            else:
                route = await self.send_route(
                    target, context, (args.get("target") or {}).get("binding_id")
                )
            operations = self.router.operations(route)
            if prior is not None and prior.prepared is not None:
                from yuki.social.content import PreparedContent

                prepared = PreparedContent(
                    prepared_transport(
                        prior.prepared.get("message") or _prepared_parts(prior.prepared)[0]
                    )
                )
            elif context.prepared_content is not None:
                from yuki.social.content import PreparedContent

                prepared = PreparedContent(prepared_transport(context.prepared_content))
            else:
                async with self.database.read_session() as reader:
                    prepared = await prepare_for_entry(
                        reader,
                        message.text,
                        platform=route.platform,
                        space_binding_id=route.binding_id if target.kind == "space" else None,
                        participant_assignment_ids=(route.binding_id, route.presence_id),
                        conversation_id=route.conversation_id,
                        operations=operations,
                    )
            mentions = prepared.message.mentions
            deferred_only = message.emoji is not None and not prepared.message.text.strip()
            ledger: list[dict[str, Any]] = []
            for mention in mentions:
                data = asdict(mention)
                data["source"] = {
                    "platform": route.platform,
                    "external_account_id": mention.external_id,
                }
                ledger.append({"type": "mention", "data": data})
            reference = None
            if args.get("reply_to_event_id") is not None:
                reference = await self.reply_reference(
                    args["reply_to_event_id"], target, route, context
                )
            if message.text:
                ledger.append({"type": "text", "data": {"text": prepared.message.text}})
            media = None
            content = message.text
            emoji_source = None
            emoji_error = None
            if deferred_only:
                if self.emoji_delivery is None:
                    emoji_error = "emoji_delivery_unavailable"
                else:
                    try:
                        emoji_source = await self.emoji_delivery.source(context, route)
                    except Exception as exc:
                        emoji_error = (
                            str(exc) if isinstance(exc, SocialError) else type(exc).__name__
                        )
                if emoji_source is not None:
                    emoji_source["reply_to_event_id"] = args.get("reply_to_event_id")
                illustration = await self._admit_emoji(
                    args, context, target, emoji_source, emoji_error
                )
                return {
                    "status": illustration["status"]
                    if illustration["status"] in {"accepted", "unknown"}
                    else "failed",
                    "emoji": illustration,
                }
            if message.path is not None:
                published = await self._path_artifact(message, args, context, target, prior)
                if published.get("error"):
                    return {
                        **published,
                        "status": "uncertain" if published.get("uncertain") else "failed",
                        "target": target.model_dump(mode="json"),
                    }
                message = message.model_copy(update={"artifact_id": UUID(published["artifact_id"])})
            if message.artifact_id is not None:
                if self.workspace is None:
                    raise SocialError("artifact_transport_unavailable")
                metadata, artifact_bytes = await asyncio.to_thread(
                    self.workspace.read_bytes, str(message.artifact_id)
                )
                if not metadata["immutable"]:
                    raise SocialError("artifact_not_immutable")
                media = TransportMedia(
                    message.attachment_kind or "file",
                    artifact_bytes,
                    metadata["name"],
                    (context.media_metadata or {}).get("mime_type")
                    or metadata.get("mime_type")
                    or "application/octet-stream",
                    context.deferred_emoji_id is not None,
                )
                ledger.append(
                    {
                        "type": message.attachment_kind,
                        "data": {
                            "artifact_id": str(message.artifact_id),
                            "name": metadata["name"],
                            "filename": metadata["name"],
                            "version": metadata["sha256"],
                            "size": metadata["size"],
                        },
                    }
                )
                if context.deferred_emoji_id is not None:
                    selected = context.media_metadata or {}
                    tags = "、".join(selected.get("tags", ()))
                    ledger[-1]["data"].update(
                        emoji_id=selected.get("id", ""),
                        summary=tags or "表情",
                        tags=list(selected.get("tags", ())),
                        is_sticker=True,
                        mime_type=media.mime_type,
                        animated=selected.get("animated", False),
                    )
                    content = f"[表情: {tags}]" if tags else "[表情]"
                else:
                    content = content or f"[文件: {metadata['name']}]"
            transport = TransportMessage(prepared.message.text, mentions, reference, media)
        receipt = await self._send_message_sequence(
            args, context, target, route, transport, tuple(ledger), prior, known_event_id
        )
        if prepared.unresolved:
            receipt["unresolved_mentions"] = list(prepared.unresolved)
        if message.emoji is not None:
            receipt.setdefault(
                "emoji", {"status": "not_accepted", "error": "emoji_delivery_unavailable"}
            )
        return receipt

    async def _admit_emoji(
        self,
        args: dict[str, Any],
        context: SocialContext,
        target: SocialTarget,
        source: dict[str, Any] | None,
        error: str | None,
    ) -> dict[str, Any]:
        if source is not None and self.emoji_delivery is not None:
            try:
                return await self.emoji_delivery.admit(args, context, target, source)
            except SQLAlchemyError:
                return {"status": "unknown", "error": "emoji_admission_unconfirmed"}
            except Exception as exc:
                error = str(exc) if isinstance(exc, SocialError) else type(exc).__name__
                logging.getLogger(__name__).warning(
                    "social_emoji_admission_failed category=%s", error
                )
            finally:
                self.emoji_delivery.wake()
        return {"status": "not_accepted", "error": error or "emoji_delivery_unavailable"}

    async def _replay_emoji(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        assert self.emoji_delivery is not None
        try:
            return await self.emoji_delivery.replay(args, context)
        except SQLAlchemyError:
            return {"status": "unknown", "error": "emoji_projection_unavailable"}

    async def _effect(
        self,
        name: str,
        args: dict[str, Any],
        context: SocialContext,
        target: SocialTarget,
        route: ResolvedSend,
        action: str,
        params: dict[str, Any],
        *,
        content: str | None = None,
        ledger_segments: tuple[dict[str, Any], ...] | None = None,
        known_event_id: int | None = None,
        emoji_request: tuple[dict[str, Any], SocialContext] | None = None,
    ) -> dict[str, Any]:
        from yuki.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None and not (capture.work_id and capture.effect_key):
            capture = None
        with model_detail("social_preparation"):
            receipt = await self.receipts.prepare(
                source_turn_id=context.turn_id,
                tool_call_id=context.call_id,
                source_conversation_id=context.conversation_id,
                action=name,
                target=target,
                payload=args,
                work_effect=capture if context.parent_operation_id is None else None,
                prepared={
                    "parts": [transport_payload(params["message"])],
                    "route": self._route_payload(route),
                    "artifact": _attachment_snapshot(ledger_segments or ()),
                }
                if action == "send"
                else None,
            )
        if action == "send" and receipt.prepared is not None:
            params = {
                "message": prepared_transport(
                    _prepared_parts(receipt.prepared)[0], params["message"].media
                )
            }
        if receipt.status is not OperationStatus.PREPARED:
            await self._record_work_delivery(receipt)
            return await self._receipt_result(receipt, context)
        if target.kind == "person" and known_event_id is None:
            known_event_id = await self.check_target(target)
        from yuki.runtime.delivery_intents import reserve
        from yuki.runtime.work_activation import current_work_control
        from yuki.runtime.work_repository import encode_json

        work_control = current_work_control.get()
        illustration: dict[str, Any] | None = None
        reservation = (
            encode_json({"target": target.model_dump(mode="json"), "arguments": args})
            if work_control is not None and name.startswith("send_") and name != "send_file_caption"
            else None
        )
        # Probes and preparation never serialize unrelated social operations.
        # The durable prepared -> executing CAS is the sole dispatch owner.
        with model_detail("social_claim"):
            async with self.database.immediate_session() as session:
                await self._validate_person_context(context, session=session)
                if context.deferred_emoji_id is not None:
                    assert self.emoji_delivery is not None
                    await self.emoji_delivery.validate_claim(
                        session, context.deferred_emoji_id, target, route
                    )
                if context.origin == "plugin_background" and context.caused_by_event_id is not None:
                    from yuki.plugin_host.notification_repository import (
                        background_authority_live,
                        publication_source_person,
                    )

                    original_event = await session.get(ChatEventModel, context.caused_by_event_id)
                    conversation = await session.get(
                        CanonicalConversationModel, context.conversation_id
                    )
                    if (
                        original_event is None
                        or conversation is None
                        or not original_event.source_plugin_id
                        or original_event.canonical_conversation_id != conversation.id
                        or not await background_authority_live(
                            session,
                            plugin_id=original_event.source_plugin_id,
                            source_person_id=publication_source_person(original_event),
                            conversation_id=context.conversation_id,
                            person_id=conversation.person_id,
                            space_id=conversation.space_id,
                        )
                    ):
                        raise SocialError("permission_denied")
                if capture is not None:
                    from yuki.runtime.work_repository import TERMINAL, WorkConflict
                    from yuki.runtime.work_schema_v1 import work

                    if work_control is not None and reservation is None:
                        await work_control.repository._assert_lease(session, work_control.lease)
                    active = await session.scalar(
                        select(work.c.id).where(
                            work.c.id == capture.work_id,
                            work.c.conversation_id == context.conversation_id,
                            work.c.state.not_in(TERMINAL),
                            work.c.generation
                            == select(CanonicalConversationModel.generation)
                            .where(CanonicalConversationModel.id == context.conversation_id)
                            .scalar_subquery(),
                        )
                    )
                    if active is None:
                        raise WorkConflict("social_work_dispatch_obsolete")
                await self._validate_self_context(context, session=session)
                await self.check_target(
                    target,
                    session=session,
                    known_event_id=known_event_id,
                )
                await self.router.validate_prepared_send(
                    session,
                    route,
                    target_kind=target.kind,
                    target_id=str(target.id),
                )
                await self._validate_reply_route(session, context, target, route)
                if emoji_request is not None and context.sequence_part_index in {None, 0}:
                    assert self.emoji_delivery is not None
                    emoji_arguments, emoji_context = emoji_request
                    source = await self.emoji_delivery.source(emoji_context, route, session=session)
                    source["after_operation_id"] = (
                        context.parent_operation_id or receipt.operation_id
                    )
                    illustration = await self.emoji_delivery.admit(
                        emoji_arguments, emoji_context, target, source, session=session
                    )
                if reservation is not None and work_control is not None:
                    await reserve(
                        work_control,
                        receipt.operation_id,
                        "artifact" if params["message"].media is not None else "message",
                        reservation,
                        session=session,
                    )
                claimed = await self.receipts.claim(
                    receipt.operation_id,
                    presence_id=route.presence_id,
                    session=session,
                    expected_payload_hash=receipt.payload_hash,
                    parent_operation_id=context.parent_operation_id,
                    sequence_part_index=context.sequence_part_index,
                )
                if claimed:
                    self.router.validate_prepared_connection(route)
        if not claimed:
            current = await self.receipts.get(receipt.operation_id)
            await self._record_work_delivery(current)
            return await self._receipt_result(current, context)
        try:
            completed = await self._dispatch_claimed(
                receipt.operation_id,
                route,
                action,
                params,
                target=target,
                context=context,
                content=content,
                ledger_segments=ledger_segments,
                reply_to_event_id=args.get("reply_to_event_id")
                if action == "send" and params["message"].reply_reference is not None
                else None,
                destination_kind=target.kind,
            )
        finally:
            if emoji_request is not None and self.emoji_delivery is not None:
                self.emoji_delivery.wake()
        result = await self._receipt_result(completed, context)
        if illustration is not None:
            result["emoji"] = illustration
        return result

    async def _dispatch_claimed(
        self,
        operation_id: str,
        route: ResolvedSend,
        action: str,
        params: dict[str, Any],
        *,
        target: SocialTarget,
        context: SocialContext,
        content: str | None,
        ledger_segments: tuple[dict[str, Any], ...] | None,
        reply_to_event_id: Any,
        destination_kind: str,
    ) -> SocialReceipt:
        """The one gateway call and receipt/ledger settlement of a claimed operation.

        Callers own preparation, authority and the durable claim; this core only
        dispatches once and records success, failure or uncertainty.
        """
        dispatched = False
        confirmed = False
        reference = None
        try:
            self.router.validate_prepared_connection(route)
            dispatched = True
            operations = self.router.operations(route)
            destination = TransportTarget(
                destination_kind, route.external_target_id, route.thread_id
            )
            with model_detail("social_transport"):
                if action == "send":
                    reference = (
                        await operations.send(route.connection.bot, destination, params["message"])
                    ).reference
                elif action == "recall":
                    await operations.recall(route.connection.bot, destination, params["reference"])
                else:
                    raise SocialError("unknown_transport_effect")
            confirmed = True
            appended = None
            from yuki.persistence.scoped_event_uow import prepare_append

            prepared_append = await prepare_append(
                content or "", segments=ledger_segments if ledger_segments is not None else ()
            )
            with model_detail("social_settlement"):
                async with self.database.immediate_session() as session:
                    from yuki.social.confirmed_content import content_sources_retained

                    if content is not None and await content_sources_retained(
                        session,
                        conversation_id=context.conversation_id,
                        target=target,
                        event_ids=(
                            context.trigger_event_id,
                            context.caused_by_event_id,
                            reply_to_event_id,
                        ),
                        person_id=getattr(context.actor, "person_id", None),
                    ):
                        scope = (
                            ConversationScope.private(
                                route.sender_account_id,
                                route.external_target_id,
                                platform=route.platform,
                                presence_id=route.presence_id,
                                conversation_id=route.conversation_id,
                            )
                            if target.kind == "person"
                            else ConversationScope.group(
                                route.sender_account_id,
                                route.external_target_id,
                                platform=route.platform,
                                presence_id=route.presence_id,
                                conversation_id=route.conversation_id,
                                thread_id=route.thread_id,
                            )
                        )
                        appended = await self.writer.append(
                            scope=scope,
                            platform_message_id=reference or f"social-operation:{operation_id}",
                            sender_user_id=route.sender_account_id,
                            direction="outbound",
                            content=content,
                            prepared=prepared_append,
                            segments=ledger_segments if ledger_segments is not None else (),
                            origin=context.origin,
                            caused_by_event_id=context.caused_by_event_id,
                            source_event_id=context.trigger_event_id or context.caused_by_event_id,
                            reply_to_message_id=(
                                params["message"].reply_reference if action == "send" else None
                            ),
                            reply_to_event_id=reply_to_event_id,
                            session=session,
                            identity_binding_id=route.binding_id
                            if target.kind == "person"
                            else None,
                            space_binding_id=route.binding_id if target.kind == "space" else None,
                        )
                    completed = await self.receipts.finish(
                        operation_id,
                        status=OperationStatus.SUCCEEDED,
                        platform_reference=reference,
                        event_id=appended.event.id if appended is not None else None,
                        session=session,
                    )
                    await self._record_work_delivery(completed, session=session)
        except BaseException as exc:
            try:
                persisted = await self.receipts.get(operation_id)
            except Exception as probe:
                exc.add_note(f"local receipt lookup failed: {type(probe).__name__}")
            else:
                if persisted.status is OperationStatus.SUCCEEDED:
                    if not isinstance(exc, Exception):
                        raise
                    await self._record_work_delivery(persisted)
                    return persisted
            try:
                with model_detail("social_settlement"):
                    async with self.database.immediate_session() as session:
                        completed = await self.receipts.finish(
                            operation_id,
                            status=OperationStatus.SUCCEEDED
                            if confirmed
                            else OperationStatus.UNCERTAIN
                            if dispatched and getattr(exc, "uncertain", True)
                            else OperationStatus.FAILED,
                            platform_reference=reference if confirmed else None,
                            error_category=None if confirmed else type(exc).__name__[:64],
                            session=session,
                        )
                        await self._record_work_delivery(completed, session=session)
            except Exception as secondary:
                exc.add_note(f"social receipt reconciliation deferred: {type(secondary).__name__}")
                raise exc from secondary
            if not isinstance(exc, Exception):
                raise
        else:
            if appended is not None:
                from yuki.execution_trace.recorder import record_confirmed_delivery

                await record_confirmed_delivery(operation_id, appended.event.id)
        return completed

    async def replay_send(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        prior = await self.receipts.find(context.turn_id, context.call_id)
        return await self._replay_receipt(args, context, prior)

    async def _replay_receipt(
        self, args: dict[str, Any], context: SocialContext, prior: SocialReceipt | None
    ) -> dict[str, Any] | None:
        if prior is not None and prior.action == "send_message_sequence":
            from yuki.social.db_models import SocialOperationModel

            if prior.payload_hash != payload_hash(
                prior.action, prior.target, args, context.conversation_id
            ):
                raise SocialError("idempotency_conflict")
            async with self.database.read_session() as session:
                dispatched = await session.scalar(
                    select(SocialOperationModel.id)
                    .where(
                        SocialOperationModel.source_turn_id
                        == select(SocialOperationModel.source_turn_id)
                        .where(SocialOperationModel.id == prior.operation_id)
                        .scalar_subquery(),
                        SocialOperationModel.tool_call_id.startswith(
                            sequence_prefix(context.call_id)
                        ),
                        SocialOperationModel.action == "send_message",
                        SocialOperationModel.status != "prepared",
                    )
                    .limit(1)
                )
            if dispatched is not None or prior.status is not OperationStatus.PREPARED:
                return await self._sequence_result(prior, context)
            return None
        if prior is None or prior.status is OperationStatus.PREPARED:
            return None
        await self.receipts.prepare(
            source_turn_id=context.turn_id,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            action="send_message",
            target=prior.target,
            payload=args,
        )
        await self._record_work_delivery(prior)
        return await self._receipt_result(prior, context)

    @staticmethod
    async def _record_work_delivery(receipt: Any, *, session: AsyncSession | None = None) -> None:
        from yuki.runtime.delivery_intents import record
        from yuki.runtime.work_activation import current_work_control

        control = current_work_control.get()
        if control is None or control.current is None or receipt.status is OperationStatus.PREPARED:
            return
        state = (
            "accepted"
            if receipt.status is OperationStatus.SUCCEEDED
            else "failed"
            if receipt.status is OperationStatus.FAILED
            else "unknown"
        )
        try:
            await record(
                control,
                receipt.operation_id,
                state,
                receipt.model_dump(mode="json"),
                session=session,
            )
        except Exception:
            logging.getLogger(__name__).exception("social_work_delivery_record_failed")

    async def _receipt_result(
        self, receipt: SocialReceipt, context: SocialContext
    ) -> dict[str, Any]:
        """Read the persisted internal event anchor; never infer it from transport data."""
        result = receipt.model_dump(mode="json")
        result.pop("platform_reference", None)
        if receipt.prepared is not None and receipt.prepared.get("artifact"):
            result.update(receipt.prepared["artifact"])
        if receipt.prepared is not None and (
            "parts" in receipt.prepared or "message" in receipt.prepared
        ):
            result["text_planned"] = bool(_prepared_parts(receipt.prepared)[0]["text"])
        if receipt.status is not OperationStatus.SUCCEEDED or receipt.action not in {
            "send_message",
            "send_file_caption",
        }:
            return result
        if receipt.prepared is not None and (
            "parts" in receipt.prepared or "message" in receipt.prepared
        ):
            accounts = _mentioned_accounts(_prepared_parts(receipt.prepared))
            if accounts:
                result["mentioned_accounts"] = accounts
        result["delivered_text_available"] = False
        if receipt.event_id is None:
            # Historical receipts prove transport but have no authoritative ledger
            # link. Missing content never grants another send or an argument replay.
            return result
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        try:
            async with self.database.read_session() as session:
                event = await session.get(ChatEventModel, receipt.event_id)
                if (
                    event is None
                    or event.canonical_conversation_id is None
                    or event.direction != "outbound"
                    or event.author_kind != "yuki"
                    or event.author_presence_id != receipt.presence_id
                    or event.event_kind != "message"
                    or event.suppression_status != "keeper"
                    or event.origin != context.origin
                    or event.caused_by_event_id != context.caused_by_event_id
                ):
                    return result
                conversation = await session.get(
                    CanonicalConversationModel, event.canonical_conversation_id
                )
                if conversation is None:
                    return result
                if receipt.target.kind == "space":
                    if conversation.space_id != str(receipt.target.id):
                        return result
                elif conversation.person_id is None or not await same_person_id(
                    session, conversation.person_id, str(receipt.target.id)
                ):
                    return result
            segments = json.loads(event.segments_json)
            # Historical speech's spoken text is visible content. File/image placeholders
            # are ledger descriptions, not text the Agent actually sent.
            text = (
                event.content
                if any(part.get("type") == "record" for part in segments)
                else "".join(
                    part.get("data", {}).get("text", "")
                    for part in segments
                    if part.get("type") == "text"
                )
            )
            result.update(delivered_text_available=True, delivered_text=text)
        except Exception:
            # A projection read must not downgrade a durable send or tempt a retry.
            logging.getLogger(__name__).exception("social_receipt_content_projection_failed")
        return result

    async def _recall(self, args: dict[str, Any], context: SocialContext) -> dict[str, Any]:
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        async with self.database.read_session() as session:
            event = await session.get(ChatEventModel, int(args["event_id"]))
            if (
                event is None
                or event.author_kind != "yuki"
                or event.direction != "outbound"
                or not event.author_presence_id
            ):
                raise SocialError("not_own_message")
            if event.platform_message_id.startswith(("social-operation:", "file:")):
                raise SocialError("message_recall_unavailable")
            conversation = await session.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
            if conversation is None:
                raise SocialError("target_not_found")
            target = SocialTarget(
                kind="person" if conversation.kind == "private" else "space",
                id=UUID(conversation.person_id or conversation.space_id or ""),
            )
        await self.check_target(target)
        route = await self.router.resolve_presence(event.author_presence_id)
        route = replace(
            route,
            external_target_id=(
                event.private_peer_user_id if target.kind == "person" else event.group_id
            )
            or "",
            conversation_id=conversation.id,
            thread_id=conversation.thread_id,
        )
        return await self._effect(
            "recall_own_message",
            args,
            context,
            target,
            route,
            "recall",
            {"reference": event.platform_message_id},
        )
