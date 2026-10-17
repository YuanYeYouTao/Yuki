"""Canonical social operations shared by chat and delegated automation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field, replace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.config_service import RuntimeConfigService
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
)
from yuki.domain.conversations import ConversationScope
from yuki.gateway.social import (
    TransportMedia,
    TransportMention,
    TransportMessage,
    TransportTarget,
)
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import (
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


@dataclass(frozen=True)
class SocialContext:
    turn_id: str
    call_id: str
    conversation_id: str
    person_refs: dict[str, str] = field(default_factory=dict)
    account_refs: dict[str, str] = field(default_factory=dict)
    space_id: str | None = None
    # Trusted internal ledger key for an immediate reply. Platform IDs are only
    # transport references and must not reconstruct an already known event.
    trigger_event_id: int | None = None
    origin: str = "social_tool"
    caused_by_event_id: int | None = None
    visible_event_ids: frozenset[int] = frozenset()
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


def _self_scene(context: SocialContext) -> bool:
    return context.initiative_run_id is not None or context.automation_run_id is not None


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
        self.emoji_delivery: Any = None

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
        """Read original message fields; ignore retired optional Work metadata."""
        # A submitted call may still contain work_report. Keep its original
        # payload for receipt identity while ignoring it as execution policy.
        outer_fields = {"target", "reply_to_event_id", "work_report"}
        if set(args) - (SocialMessage.model_fields.keys() | outer_fields):
            raise SocialError("invalid_message_arguments")
        return {key: value for key, value in args.items() if key in SocialMessage.model_fields}

    @staticmethod
    def _canonical_send_arguments(args: dict[str, Any]) -> dict[str, Any]:
        """Normalize model-owned text once before receipts or transport planning."""

        SocialService._message_arguments(args)
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
            canonical.get(key) for key in ("artifact_id", "emoji", "mentions")
        ):
            raise SocialError("empty_message_after_sanitization")
        SocialMessage.model_validate(SocialService._message_arguments(canonical))
        return canonical

    async def _binding_choice(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        selected = args.get("target")
        if (
            not isinstance(selected, dict)
            or selected.get("kind") != "person"
            or selected.get("binding_id")
        ):
            return None
        async with self.database.sessions() as session:
            platform = await session.scalar(
                select(IdentityBindingModel.platform)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.identity_binding_id == IdentityBindingModel.id,
                )
                .where(CanonicalConversationModel.id == context.conversation_id)
            )
        if platform != "app":
            return None
        target = await self.target("person", selected, context)
        await self.check_target(target)
        async with self.database.sessions() as session:
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
        async with self.database.sessions() as session:
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

    async def target(self, kind: str, args: dict[str, Any], context: SocialContext) -> SocialTarget:
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
            async with self.database.sessions() as session:
                raw = await current_person_id(session, str(target.id))
            target = target.model_copy(update={"id": UUID(raw)})
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
                person = await session.get(CanonicalPersonModel, str(target.id))
                known_query = select(ChatEventModel.id).where(
                    person_family_condition(ChatEventModel.author_person_id, str(target.id)),
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.author_kind == "person",
                )
                if known_event_id is not None:
                    known_query = known_query.where(ChatEventModel.id == known_event_id)
                known = await session.scalar(known_query.limit(1))
                if person is None or not person.enabled or known is None:
                    raise SocialError("contact_not_allowed")
                return int(known)
            else:
                space = await session.get(CanonicalSpaceModel, str(target.id))
                if space is None or not space.enabled:
                    raise SocialError("space_not_allowed")
                return None

    async def route(self, target: SocialTarget) -> ResolvedSend:
        # PresenceRouter may provision a missing route; explicit social operations must not.
        async with self.database.sessions() as session:
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
        async with self.database.sessions() as session:
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
        async with self.database.sessions() as session:
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
                presence_id=context.reply_presence_id
                or context.presence_id
                or (event.ingress_presence_id if event is not None else None),
            )
        if binding_id is not None:
            if target.kind != "person":
                raise SocialError("binding_requires_person")
            return await self.router.resolve_send_for_binding(str(target.id), binding_id)
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
        if isinstance(event_id, bool) or event_id not in context.visible_event_ids:
            raise SocialError("reply_event_not_visible")
        async with self.database.sessions() as session:
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
        async with self.database.sessions() as session:
            presence = await session.get(PresenceModel, event.ingress_presence_id)
        if presence is None or presence.platform != route.platform:
            raise SocialError("reply_event_unavailable")
        return event.platform_message_id

    async def person_binding(
        self,
        target: SocialTarget,
        args: dict[str, Any],
        context: SocialContext,
        *,
        platform: str,
    ) -> IdentityBindingModel:
        async with self.database.sessions() as session:
            query = select(IdentityBindingModel).where(
                person_family_condition(IdentityBindingModel.person_id, str(target.id)),
                IdentityBindingModel.status == "active",
                IdentityBindingModel.platform == platform,
            )
            bindings = list(await session.scalars(query))
        if args.get("binding_id"):
            bindings = [b for b in bindings if b.id == str(args["binding_id"])]
        account = context.account_refs.get(str(args.get("subject_ref", "")))
        if account is not None:
            bindings = [b for b in bindings if b.external_account_id == account]
        if len(bindings) != 1:
            raise SocialError("binding_ambiguous" if bindings else "binding_unavailable")
        return bindings[0]

    async def require_member(self, route: ResolvedSend, account: str) -> None:
        try:
            member = await self.router.operations(route).is_member(
                route.connection.bot, route.external_target_id, account
            )
        except Exception as exc:
            raise SocialError("group_member_unavailable") from exc
        if not member:
            raise SocialError("group_member_unavailable")

    async def _send_message_sequence(
        self,
        args: dict[str, Any],
        context: SocialContext,
        target: SocialTarget,
        route: ResolvedSend,
        message: TransportMessage,
        ledger: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        """Plan protocol parts before dispatch; every actual send owns its receipt."""
        prior = await self.receipts.find(context.turn_id, context.call_id)
        snapshot = context.runtime_snapshot
        if snapshot is None and self.runtime_config is not None:
            snapshot = await self.runtime_config.snapshot()
        layout: tuple[TransportMessage, ...] = (message,)
        if message.media is None and message.text and snapshot is not None:
            layout = tuple(
                replace(
                    message,
                    text=chunk,
                    mentions=message.mentions if index == 0 else (),
                    reply_reference=message.reply_reference if index == 0 else None,
                )
                for index, chunk in enumerate(
                    OutboundMessageSplitter.render(message.text, runtime=snapshot)
                )
            )
        operations = self.router.operations(route)
        planned = tuple(part for item in layout for part in operations.plan_send(item))

        def arguments(part: TransportMessage, index: int) -> dict[str, Any]:
            value = dict(args, text=part.text)
            if part.media is None:
                for field in ("artifact_id", "attachment_kind", "emoji"):
                    value.pop(field, None)
            if index and not part.mentions:
                value.pop("mentions", None)
            if index and part.reply_reference is None:
                value.pop("reply_to_event_id", None)
            return value

        def segments(part: TransportMessage) -> tuple[dict[str, Any], ...]:
            result = []
            for item in ledger:
                if item["type"] == "text":
                    if part.text:
                        result.append({"type": "text", "data": {"text": part.text}})
                elif item["type"] == "mention":
                    if part.mentions:
                        result.append(item)
                elif item["type"] not in {"image", "file"} or part.media is not None:
                    result.append(item)
            return tuple(result)

        async def send(
            part: TransportMessage, part_context: SocialContext, payload: dict[str, Any]
        ) -> dict[str, Any]:
            content = part.text
            if not content and part.media is not None and part.media.kind == "file":
                content = f"[文件: {part.media.filename}]"
            return await self._effect(
                "send_message",
                payload,
                part_context,
                target,
                route,
                "send",
                {"message": part},
                content=content,
                ledger_segments=segments(part),
            )

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
        parent, dispatch = await self.receipts.prepare_sequence(
            source_turn_id=context.turn_id,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            target=target,
            payload=args,
            parts=tuple(arguments(part, index) for index, part in enumerate(planned)),
            legacy_payload=await self._legacy_sequence_payload(args, context),
            work_effect=capture if context.parent_operation_id is None else None,
        )
        if dispatch:
            prefix = sequence_prefix(context.call_id)
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
                    result = await send(part, part_context, arguments(part, index))
                except Exception as exc:
                    if not index:
                        raise
                    # A later preflight cannot erase a confirmed original part.
                    logging.getLogger(__name__).warning(
                        "social_sequence_preflight_failed category=%s", type(exc).__name__
                    )
                    break
                if result.get("status") != OperationStatus.SUCCEEDED.value:
                    break
        return await self._sequence_result(
            parent, context, file=args.get("attachment_kind") == "file"
        )

    async def _sequence_result(
        self, parent: SocialReceipt, context: SocialContext, *, file: bool = False
    ) -> dict[str, Any]:
        async with self.database.sessions() as session:
            result = await self.receipts.delivery_facts(
                session,
                parent.operation_id,
                conversation_id=context.conversation_id,
                tool_call_id=context.call_id,
                planned_file_parts=None,
            )
        assert result is not None
        for part in result["parts"]:
            if part.get("operation_id"):
                receipt = await self.receipts.get(part["operation_id"])
                part.update(await self._receipt_result(receipt, context))
        if result["status"] != "succeeded":
            last: dict[str, Any] = next(
                (part for part in result["parts"] if part["status"] != "succeeded"), {}
            )
            result["error"] = last.get("error_category") or (
                "delivery_uncertain" if result["status"] == "uncertain" else "delivery_failed"
            )
        if file and result.get("parts"):
            # Aliases describe the original effects; they never dispatch another send.
            parts = result["parts"]
            result["file"] = parts[0]
            result["caption"] = (
                parts[1] if len(parts) == 2 else {"status": result["status"], "parts": parts[1:]}
            )
            if parts[0]["status"] == "succeeded" and result["status"] != "succeeded":
                result["error"] = "file_sent_caption_unconfirmed"
        return result

    async def execute(
        self, name: str, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any]:
        from yuki.social.source_keys import social_call_key

        context = replace(context, call_id=social_call_key(context.call_id))
        if name == "send_message" and "voice" in args:
            # Retired calls may already have crossed the dispatch boundary. Bind
            # those facts to the original payload before applying today's schema.
            prior = await self.receipts.find(context.turn_id, context.call_id)
            if prior is not None and prior.status is not OperationStatus.PREPARED:
                await self.receipts.prepare(
                    source_turn_id=context.turn_id,
                    tool_call_id=context.call_id,
                    source_conversation_id=context.conversation_id,
                    action=name,
                    target=prior.target,
                    payload=args,
                )
                await self._record_work_delivery(prior)
                return await self._receipt_result(prior, context)
            raise SocialError("invalid_message_arguments")
        if name == "send_message":
            args = self._canonical_send_arguments(args)
            replay = await self.replay_send(args, context)
            if replay is not None:
                return replay
            await self._validate_person_context(context)
            choice = await self._binding_choice(args, context)
            if choice is not None:
                return choice
        await self._validate_self_context(context)
        if name != "send_message":
            await self._validate_person_context(context)
        if name == "read_conversation_history":
            from yuki.social.history import read_history

            return await read_history(self, args, context)
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
            async with self.database.sessions() as session:
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
        if name == "send_message":
            replay = await self.replay_send(args, context)
            if replay is not None:
                return replay
            selected = args.get("target") or {}
            if not isinstance(selected, dict):
                raise SocialError("invalid_message_arguments")
            kind = selected.get("kind") or ("space" if context.space_id else "person")
            if kind not in {"person", "space"}:
                raise SocialError("invalid_target_kind")
            if args.get("target") and not any(
                selected.get(key) for key in ("target_id", "display_name", "subject_ref")
            ):
                raise SocialError("target_selector_conflict")
            target_args = selected
        elif name == "get_group_members":
            kind = "space"
            target_args = args
        else:
            raise SocialError("unknown_tool")
        target = await self.target(kind, target_args, context)
        if name == "get_group_members":
            # Telegram cannot enumerate every group member. The common contract
            # exposes the actual canonical people observed in this Space.
            async with self.database.sessions() as session:
                rows = (
                    await session.execute(
                        select(
                            CanonicalPersonModel.id,
                            CanonicalPersonModel.display_name,
                        )
                        .join(
                            ChatEventModel,
                            person_family_condition(
                                ChatEventModel.author_person_id, CanonicalPersonModel.id
                            ),
                        )
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id
                            == ChatEventModel.canonical_conversation_id,
                        )
                        .where(
                            CanonicalConversationModel.space_id == str(target.id),
                            ChatEventModel.author_kind == "person",
                            CanonicalPersonModel.enabled.is_(True),
                            CanonicalPersonModel.merged_into_person_id.is_(None),
                        )
                        .group_by(CanonicalPersonModel.id)
                        .order_by(CanonicalPersonModel.id)
                    )
                ).all()
            offset = int(args.get("cursor") or 0)
            limit = max(1, min(100, int(args.get("limit", 20))))
            if offset < 0:
                raise SocialError("invalid_cursor")
            return {
                "items": [
                    {"person_id": row[0], "display_name": row[1] or "成员"}
                    for row in rows[offset : offset + limit]
                ],
                "coverage": "observed",
                "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
            }
        message = SocialMessage.model_validate(self._message_arguments(args))
        await self.check_target(target)
        route = await self.send_route(
            target,
            context,
            (args.get("target") or {}).get("binding_id"),
        )
        if message.mentions and target.kind != "space":
            raise SocialError("mentions_require_group")
        mentions = []
        ledger: list[dict[str, Any]] = []
        for mention in message.mentions:
            selectors = mention.model_dump(mode="json", exclude_none=True)
            person = await self.target("person", selectors, context)
            member = await self.person_binding(person, selectors, context, platform=route.platform)
            await self.require_member(route, member.external_account_id)
            mentions.append(
                TransportMention(member.external_account_id, member.display_name or "成员")
            )
            ledger.append({"type": "mention", "data": {"person_id": str(person.id)}})
        reference = None
        if args.get("reply_to_event_id") is not None:
            reference = await self.reply_reference(
                args["reply_to_event_id"], target, route, context
            )
        if message.text:
            ledger.append({"type": "text", "data": {"text": message.text}})
        media = None
        content = message.text
        prepared_emoji = None
        if message.artifact_id is not None:
            if self.workspace is None:
                raise SocialError("artifact_transport_unavailable")
            metadata, data = await asyncio.to_thread(
                self.workspace.read_bytes, str(message.artifact_id)
            )
            if not metadata["immutable"]:
                raise SocialError("artifact_not_immutable")
            media = TransportMedia(
                message.attachment_kind or "file",
                data,
                metadata["name"],
                metadata.get("mime_type") or "application/octet-stream",
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
            content = content or f"[文件: {metadata['name']}]"
        elif message.emoji is not None:
            if context.runtime_snapshot is None or self.emoji_delivery is None:
                raise SocialError("emoji_delivery_unavailable")
            from yuki.emoji.models import (
                EmojiDeliveryRequest,
                EmojiPlacement,
                EmojiPreparationStatus,
                EmojiReplyMode,
            )

            prepared_emoji = await self.emoji_delivery.prepare(
                EmojiDeliveryRequest(
                    mode=EmojiReplyMode.PREFERRED,
                    placement=EmojiPlacement.AFTER_TEXT,
                    goal=message.emoji.goal,
                    emotion=message.emoji.emotion,
                    explicit_request=True,
                ),
                person_id=str(target.id) if target.kind == "person" else None,
                space_id=str(target.id) if target.kind == "space" else None,
                response_text=message.text,
                runtime=context.runtime_snapshot,
            )
            if (
                prepared_emoji.status is not EmojiPreparationStatus.READY
                or prepared_emoji.message is None
            ):
                raise SocialError("emoji_" + prepared_emoji.reason_code)
            selected_media = prepared_emoji.message.media[0]
            media = TransportMedia(
                "image", selected_media.content, "emoji", selected_media.mime_type, True
            )
            ledger.append(
                {
                    "type": "image",
                    "data": {
                        "emoji_id": selected_media.emoji_id or "",
                        "summary": selected_media.summary[:2000],
                        "mime_type": selected_media.mime_type,
                        "animated": selected_media.animated,
                    },
                }
            )
        transport = TransportMessage(message.text, tuple(mentions), reference, media)
        separate = (
            media is not None
            and media.kind == "file"
            and self.router.operations(route).file_caption_separate
        )
        if separate and reference:
            raise SocialError("file_quote_unavailable")
        receipt = await self._send_message_sequence(
            args, context, target, route, transport, tuple(ledger)
        )
        if prepared_emoji is not None and receipt.get("status") == OperationStatus.SUCCEEDED:
            try:
                await self.emoji_delivery.record_send_accepted(
                    prepared_emoji.message, source="agent_delivery"
                )
                await self.emoji_delivery.record_success(
                    prepared_emoji.message,
                    actor_person_id=str(target.id) if target.kind == "person" else None,
                    space_id=str(target.id) if target.kind == "space" else None,
                    trigger_message_id=context.inbound.message_id
                    if context.inbound is not None
                    else "",
                    source="agent",
                    ledger_recorded=True,
                )
            except Exception:
                logging.getLogger(__name__).exception("social_media_post_send_record_failed")
        return receipt

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
    ) -> dict[str, Any]:
        from yuki.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None and not (capture.work_id and capture.effect_key):
            capture = None
        receipt = await self.receipts.prepare(
            source_turn_id=context.turn_id,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            action=name,
            target=target,
            payload=args,
            work_effect=capture if context.parent_operation_id is None else None,
        )
        if receipt.status is not OperationStatus.PREPARED:
            await self._record_work_delivery(receipt)
            return await self._receipt_result(receipt, context)
        known_event_id = await self.check_target(target)
        fresh = (
            await self.router.resolve_presence(route.presence_id)
            if name == "recall_own_message"
            else await self.send_route(
                target,
                context,
                (args.get("target") or {}).get("binding_id")
                if name in {"send_message", "send_file_caption"}
                else None,
            )
        )
        if (
            fresh.presence_id,
            fresh.binding_id,
            fresh.route_generation,
            fresh.connection.snapshot,
            None if name == "recall_own_message" else fresh.conversation_id,
            None if name == "recall_own_message" else fresh.thread_id,
        ) != (
            route.presence_id,
            route.binding_id,
            route.route_generation,
            route.connection.snapshot,
            None if name == "recall_own_message" else route.conversation_id,
            None if name == "recall_own_message" else route.thread_id,
        ):
            raise SocialError("route_changed")
        from yuki.runtime.delivery_intents import reserve
        from yuki.runtime.work_activation import current_work_control

        work_control = current_work_control.get()
        if work_control is not None and name.startswith("send_") and name != "send_file_caption":
            await reserve(
                work_control,
                receipt.operation_id,
                "artifact" if args.get("artifact_id") else "message",
                {"target": target.model_dump(mode="json"), "arguments": args},
            )
        # Probes and preparation never serialize unrelated social operations.
        # The durable prepared -> executing CAS is the sole dispatch owner.
        async with self.database.immediate_session() as session:
            await self._validate_person_context(context, session=session)
            if context.origin == "plugin_background" and context.caused_by_event_id is not None:
                from yuki.plugin_host.notification_repository import background_authority_live

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
                        conversation_id=context.conversation_id,
                        person_id=conversation.person_id,
                        space_id=conversation.space_id,
                    )
                ):
                    raise SocialError("permission_denied")
            if capture is not None:
                from yuki.runtime.work_repository import TERMINAL, WorkConflict
                from yuki.runtime.work_schema_v1 import work

                if work_control is not None:
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
        await self._dispatch_claimed(
            receipt.operation_id,
            route,
            action,
            params,
            target=target,
            context=context,
            content=content,
            ledger_segments=ledger_segments,
            reply_to_event_id=args.get("reply_to_event_id"),
            destination_kind=target.kind,
        )
        completed = await self.receipts.get(receipt.operation_id)
        await self._record_work_delivery(completed)
        return await self._receipt_result(completed, context)

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
    ) -> None:
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
                        segments=ledger_segments if ledger_segments is not None else (),
                        sender_is_bot=True,
                        origin=context.origin,
                        caused_by_event_id=context.caused_by_event_id,
                        source_event_id=context.trigger_event_id or context.caused_by_event_id,
                        reply_to_message_id=(
                            params["message"].reply_reference if action == "send" else None
                        ),
                        reply_to_event_id=reply_to_event_id,
                        session=session,
                        identity_binding_id=route.binding_id if target.kind == "person" else None,
                    )
                await self.receipts.finish(
                    operation_id,
                    status=OperationStatus.SUCCEEDED,
                    platform_reference=reference,
                    event_id=appended.event.id if appended is not None else None,
                    session=session,
                )
        except BaseException as exc:
            try:
                persisted = await self.receipts.get(operation_id)
            except Exception as probe:
                exc.add_note(f"local receipt lookup failed: {type(probe).__name__}")
            else:
                if persisted.status is OperationStatus.SUCCEEDED:
                    if not isinstance(exc, Exception):
                        raise
                    return
            try:
                async with self.database.sessions() as session, session.begin():
                    await self.receipts.finish(
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
            except Exception as secondary:
                exc.add_note(f"social receipt reconciliation deferred: {type(secondary).__name__}")
                raise exc from secondary
            if not isinstance(exc, Exception):
                raise
        else:
            if appended is not None:
                from yuki.execution_trace.recorder import record_confirmed_delivery

                await record_confirmed_delivery(operation_id, appended.event.id)

    async def _legacy_sequence_payload(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        """Only old plaintext parents included their original layout in the digest."""
        if args.get("artifact_id") or args.get("emoji"):
            return None
        snapshot = context.runtime_snapshot
        if snapshot is None and self.runtime_config is not None:
            snapshot = await self.runtime_config.snapshot()
        chunks = (
            OutboundMessageSplitter.render(str(args.get("text", "")), runtime=snapshot)
            if snapshot is not None
            else (str(args.get("text", "")),)
        )
        return {"original": args, "chunks": chunks}

    async def replay_send(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        prior = await self.receipts.find(context.turn_id, context.call_id)
        if prior is not None and prior.action == "send_message_sequence":
            from yuki.social.db_models import SocialOperationModel

            legacy = await self._legacy_sequence_payload(args, context)
            hashes = {payload_hash(prior.action, prior.target, args, context.conversation_id)}
            if legacy is not None:
                hashes.add(
                    payload_hash(prior.action, prior.target, legacy, context.conversation_id)
                )
            if prior.payload_hash not in hashes:
                raise SocialError("idempotency_conflict")
            async with self.database.sessions() as session:
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
            if dispatched is not None:
                return await self._sequence_result(
                    prior, context, file=args.get("attachment_kind") == "file"
                )
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
        if args.get("attachment_kind") == "file" and (args.get("text") or args.get("mentions")):
            async with self.database.sessions() as session:
                presence = await session.get(PresenceModel, prior.presence_id)
            if presence is not None and presence.platform == "qq":
                return await self._file_result(prior.operation_id, context)
        return await self._receipt_result(prior, context)

    @staticmethod
    async def _record_work_delivery(receipt: Any) -> None:
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
            await record(control, receipt.operation_id, state, receipt.model_dump(mode="json"))
        except Exception:
            logging.getLogger(__name__).exception("social_work_delivery_record_failed")

    async def _receipt_result(
        self, receipt: SocialReceipt, context: SocialContext
    ) -> dict[str, Any]:
        """Read the persisted internal event anchor; never infer it from transport data."""
        result = receipt.model_dump(mode="json")
        result.pop("platform_reference", None)
        if receipt.status is not OperationStatus.SUCCEEDED or receipt.action not in {
            "send_message",
            "send_file_caption",
        }:
            return result
        result["delivered_text_available"] = False
        if receipt.event_id is None:
            # Historical receipts prove transport but have no authoritative ledger
            # link. Missing content never grants another send or an argument replay.
            return result
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        try:
            async with self.database.sessions() as session:
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
                if conversation is None or (
                    conversation.space_id
                    if receipt.target.kind == "space"
                    else conversation.person_id
                ) != str(receipt.target.id):
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

    async def _file_result(self, operation_id: str, context: SocialContext) -> dict[str, Any]:
        file = await self.receipts.get(operation_id)
        caption = await self.receipts.find(f"social-caption:{operation_id}", "caption")
        result = await self._receipt_result(file, context)
        result["file"] = dict(result)
        result["caption"] = (
            await self._receipt_result(caption, context) if caption else {"status": "not_sent"}
        )
        if file.status is OperationStatus.SUCCEEDED and (
            caption is None or caption.status is not OperationStatus.SUCCEEDED
        ):
            result["status"] = (
                "uncertain"
                if caption
                and caption.status in {OperationStatus.EXECUTING, OperationStatus.UNCERTAIN}
                else "failed"
            )
            result["error"] = "file_sent_caption_unconfirmed"
            result["retryable"] = False
        return result

    async def _recall(self, args: dict[str, Any], context: SocialContext) -> dict[str, Any]:
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        async with self.database.sessions() as session:
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
