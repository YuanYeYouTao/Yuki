"""Canonical social operations shared by chat and delegated automation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.canonical_db_models import PersonActiveRouteModel, SpaceActiveRouteModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.gateway.providers.napcat import NapCatProvider
from qq_ai_bot.gateway.providers.snowluma import SnowLumaProvider
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.routing import PresenceRouter, ResolvedSend
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialMessage, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository
from qq_ai_bot.social.transfer import ArtifactTransfer


@dataclass(frozen=True)
class SocialContext:
    turn_id: str
    call_id: str
    conversation_id: str
    person_refs: dict[str, str] = field(default_factory=dict)
    account_refs: dict[str, str] = field(default_factory=dict)
    space_id: str | None = None
    # Backend-only proof from the current private inbound event, never tool arguments.
    reply_message_id: str | None = None
    reply_presence_id: str | None = None


class SocialService:
    def __init__(
        self, database: Database, router: PresenceRouter, writer: ScopedEventLedgerUnitOfWork
    ) -> None:
        self.database = database
        self.router = router
        self.writer = writer
        self.receipts = SocialOperationRepository(database)
        self._lock = asyncio.Lock()
        self.runtime_config: RuntimeConfigService | None = None
        self.transfer: ArtifactTransfer | None = None

    @staticmethod
    async def _call(route: ResolvedSend, action: str, params: dict[str, Any]) -> Any:
        provider = {"napcat": NapCatProvider(), "snowluma": SnowLumaProvider()}.get(
            route.connection.snapshot.provider
        )
        if provider is None:
            raise SocialError("capability_unavailable")
        async with asyncio.timeout(30):
            return await provider.social_action(route.connection.bot, action, params)

    async def contacts(
        self, kind: str, query: str = "", *, exact: bool = False
    ) -> list[dict[str, str]]:
        async with self.database.sessions() as session:
            if kind == "person":
                known = (
                    select(ChatEventModel.id)
                    .where(
                        ChatEventModel.author_person_id == CanonicalPersonModel.id,
                        ChatEventModel.direction == "inbound",
                        ChatEventModel.author_kind == "person",
                    )
                    .exists()
                )
                rows = (
                    await session.execute(
                        select(CanonicalPersonModel.id, IdentityBindingModel.display_name)
                        .join(
                            IdentityBindingModel,
                            IdentityBindingModel.person_id == CanonicalPersonModel.id,
                        )
                        .where(
                            CanonicalPersonModel.enabled.is_(True),
                            known,
                            IdentityBindingModel.status == "active",
                            IdentityBindingModel.platform == "qq",
                            IdentityBindingModel.display_name == query
                            if exact
                            else IdentityBindingModel.display_name.contains(query, autoescape=True),
                        )
                        .order_by(CanonicalPersonModel.id, IdentityBindingModel.id)
                        .limit(20)
                    )
                ).all()
            elif kind == "space":
                rows = (
                    await session.execute(
                        select(CanonicalSpaceModel.id, CanonicalSpaceModel.name)
                        .where(
                            CanonicalSpaceModel.enabled.is_(True),
                            CanonicalSpaceModel.name == query
                            if exact
                            else CanonicalSpaceModel.name.contains(query, autoescape=True),
                        )
                        .order_by(CanonicalSpaceModel.id)
                        .limit(20)
                    )
                ).all()
            else:
                raise SocialError("invalid_target_kind")
        return list(
            {
                str(row[0]): {"target_id": str(row[0]), "display_name": str(row[1]), "kind": kind}
                for row in rows
            }.values()
        )

    async def target(self, kind: str, args: dict[str, Any], context: SocialContext) -> SocialTarget:
        selectors = [key for key in ("target_id", "display_name", "subject_ref") if args.get(key)]
        if not selectors and kind == "space" and context.space_id:
            raw = context.space_id
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
                row
                for row in await self.contacts(kind, str(args["display_name"]), exact=True)
                if row["display_name"] == args["display_name"]
            ]
            if len(matches) != 1:
                raise SocialError("target_ambiguous" if matches else "target_not_found")
            raw = matches[0]["target_id"]
        else:
            raw = str(args["target_id"])
        target = SocialTarget.model_validate({"kind": kind, "id": raw})
        await self.check_target(target)
        return target

    async def check_target(self, target: SocialTarget, *, sending: bool = False) -> None:
        async with self.database.sessions() as session:
            if target.kind == "person":
                person = await session.get(CanonicalPersonModel, str(target.id))
                known = await session.scalar(
                    select(ChatEventModel.id)
                    .where(
                        ChatEventModel.author_person_id == str(target.id),
                        ChatEventModel.direction == "inbound",
                        ChatEventModel.author_kind == "person",
                    )
                    .limit(1)
                )
                if person is None or not person.enabled or known is None:
                    raise SocialError("contact_not_allowed")
            else:
                space = await session.get(CanonicalSpaceModel, str(target.id))
                if space is None or not space.enabled or (sending and not space.autonomous_enabled):
                    raise SocialError("space_not_allowed")

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
        if result.platform != "qq":
            raise SocialError("capability_unavailable")
        async with self.database.sessions() as session:
            binding_model = IdentityBindingModel if target.kind == "person" else SpaceBindingModel
            binding = cast(
                IdentityBindingModel | SpaceBindingModel | None,
                await session.get(binding_model, result.binding_id),
            )
            if binding is None or binding.status != "active":
                raise SocialError("binding_unavailable")
        return result

    async def send_route(self, target: SocialTarget, context: SocialContext) -> ResolvedSend:
        if target.kind != "person" or context.reply_message_id is None:
            return await self.route(target)
        async with self.database.sessions() as session:
            event = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == context.conversation_id,
                    ChatEventModel.platform_message_id == context.reply_message_id,
                    ChatEventModel.ingress_presence_id == context.reply_presence_id,
                    ChatEventModel.scope_type == "private",
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.author_kind == "person",
                )
            )
            if event is None:
                raise SocialError("invalid_reply_context")
            if event.author_person_id != str(target.id):
                return await self.route(target)
            presence = await session.get(PresenceModel, context.reply_presence_id)
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.person_id == str(target.id),
                    IdentityBindingModel.platform == "qq",
                    IdentityBindingModel.external_account_id == event.sender_user_id,
                    IdentityBindingModel.status == "active",
                )
            )
            if presence is None or not presence.enabled or binding is None:
                raise SocialError("reply_identity_unavailable")
            account, binding_id, peer = (
                presence.external_account_id,
                binding.id,
                binding.external_account_id,
            )
        route = await self.router.resolve_send_for_account(account)
        if route.presence_id != context.reply_presence_id:
            raise SocialError("reply_presence_changed")
        return replace(
            route,
            binding_id=binding_id,
            external_target_id=peer,
            kind="private",
            route_generation=0,
        )

    async def person_binding(
        self,
        target: SocialTarget,
        args: dict[str, Any],
        context: SocialContext,
        *,
        private_route: ResolvedSend | None = None,
    ) -> IdentityBindingModel:
        async with self.database.sessions() as session:
            bindings = list(
                await session.scalars(
                    select(IdentityBindingModel).where(
                        IdentityBindingModel.person_id == str(target.id),
                        IdentityBindingModel.platform == "qq",
                        IdentityBindingModel.status == "active",
                    )
                )
            )
        if args.get("binding_id"):
            bindings = [b for b in bindings if b.id == str(args["binding_id"])]
        account = context.account_refs.get(str(args.get("subject_ref", "")))
        if account is not None:
            bindings = [b for b in bindings if b.external_account_id == account]
        if private_route is not None:
            bindings = [b for b in bindings if b.id == private_route.binding_id]
        if len(bindings) != 1:
            raise SocialError("binding_ambiguous" if bindings else "binding_unavailable")
        return bindings[0]

    async def require_member(self, route: ResolvedSend, account: str) -> None:
        try:
            result = await self._call(
                route,
                "get_group_member_info",
                {
                    "group_id": int(route.external_target_id),
                    "user_id": int(account),
                    "no_cache": True,
                },
            )
        except Exception as exc:
            raise SocialError("group_member_unavailable") from exc
        if not isinstance(result, dict) or str(result.get("user_id")) != account:
            raise SocialError("group_member_unavailable")

    async def execute(
        self, name: str, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any]:
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
                for item in items:
                    if kind == "person":
                        bindings = (
                            await session.scalars(
                                select(IdentityBindingModel)
                                .where(
                                    IdentityBindingModel.person_id == item["target_id"],
                                    IdentityBindingModel.platform == "qq",
                                    IdentityBindingModel.status == "active",
                                )
                                .order_by(IdentityBindingModel.id)
                            )
                        ).all()
                        item["bindings"] = [
                            {"binding_id": b.id, "user_id": b.external_account_id} for b in bindings
                        ]
                    else:
                        spaces = (
                            await session.scalars(
                                select(SpaceBindingModel)
                                .where(
                                    SpaceBindingModel.space_id == item["target_id"],
                                    SpaceBindingModel.platform == "qq",
                                    SpaceBindingModel.status == "active",
                                )
                                .order_by(SpaceBindingModel.id)
                            )
                        ).all()
                        item["bindings"] = [
                            {"binding_id": b.id, "group_id": b.external_space_id} for b in spaces
                        ]
            return (
                items[0] if args.get("target_id") or args.get("subject_ref") else {"items": items}
            )
        if name == "recall_own_message":
            return await self._recall(args, context)
        kind = "space" if name in {"send_group_message", "get_group_members"} else "person"
        target = await self.target(kind, args, context)
        if name == "get_group_members":
            routes = await self.router.accessible_group_connections(
                str(target.id), binding_id=args.get("space_binding_id")
            )
            rows = None
            for route in routes:
                try:
                    result = await self._call(
                        route, "get_group_member_list", {"group_id": int(route.external_target_id)}
                    )
                    if isinstance(result, list):
                        rows = result
                        break
                except Exception:
                    continue
            if not isinstance(rows, list):
                raise SocialError("invalid_provider_result")
            offset = int(args.get("cursor") or 0)
            limit = max(1, min(100, int(args.get("limit", 20))))
            if offset < 0:
                raise SocialError("invalid_cursor")
            rows = sorted(
                (row for row in rows if isinstance(row, dict)),
                key=lambda row: str(row.get("user_id", "")),
            )
            items = [
                {
                    "user_id": str(row.get("user_id", "")),
                    "display_name": str(row.get("card") or row.get("nickname") or "")[:128],
                }
                for row in rows[offset : offset + limit]
            ]
            return {
                "items": items,
                "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
            }
        if name not in {"send_private_message", "send_group_message", "poke_person"}:
            raise SocialError("unknown_tool")
        message = (
            None
            if name == "poke_person"
            else SocialMessage.model_validate(
                {
                    key: value
                    for key, value in args.items()
                    if key in {"text", "artifact_id", "attachment_kind", "mentions"}
                }
            )
        )
        route_target = target
        if name == "poke_person":
            scene = args.get("scene", "current")
            if scene not in {"current", "private"} or (scene == "private" and args.get("space_id")):
                raise SocialError("invalid_poke_scene")
            space_id = (args.get("space_id") or context.space_id) if scene == "current" else None
            if space_id:
                try:
                    route_target = SocialTarget.model_validate({"kind": "space", "id": space_id})
                except ValueError:
                    raise SocialError("invalid_space_id") from None
        await self.check_target(route_target, sending=True)
        route = await self.send_route(route_target, context)
        params: dict[str, Any]
        if name == "poke_person":
            binding = await self.person_binding(
                target,
                args,
                context,
                private_route=route if route_target.kind == "person" else None,
            )
            params = {"user_id": int(binding.external_account_id)}
            if route_target.kind == "space":
                await self.require_member(route, binding.external_account_id)
                params["group_id"] = int(route.external_target_id)
            action = "send_poke"
        else:
            assert message is not None
            action = "send_private_msg" if target.kind == "person" else "send_group_msg"
            params = {
                "user_id" if target.kind == "person" else "group_id": int(route.external_target_id),
                "message": [{"type": "text", "data": {"text": message.text}}],
            }
            if message.mentions:
                if target.kind != "space":
                    raise SocialError("mentions_require_group")
                segments = []
                for mention in message.mentions:
                    selectors = mention.model_dump(mode="json", exclude_none=True)
                    person = await self.target("person", selectors, context)
                    member = await self.person_binding(person, selectors, context)
                    await self.require_member(route, member.external_account_id)
                    segments.append({"type": "at", "data": {"qq": member.external_account_id}})
                params["message"] = segments + params["message"]
        if message is not None and message.artifact_id is not None:
            prior = await self.receipts.find(context.turn_id, context.call_id)
            if prior is not None and prior.status is not OperationStatus.PREPARED:
                # Validate the payload hash before returning a replay, without
                # requiring an expired/deleted workspace artifact to still exist.
                await self.receipts.prepare(
                    source_turn_id=context.turn_id,
                    tool_call_id=context.call_id,
                    source_conversation_id=context.conversation_id,
                    action=name,
                    target=target,
                    payload=args,
                )
                return (
                    await self._file_result(prior.operation_id)
                    if message.attachment_kind == "file" and (message.text or message.mentions)
                    else prior.model_dump(mode="json")
                )
            if self.transfer is None:
                raise SocialError("artifact_transport_unavailable")
            async with self.transfer.prepare(str(message.artifact_id)) as (metadata, path):
                ledger_segments = (
                    {
                        "type": message.attachment_kind,
                        "data": {"artifact_id": str(message.artifact_id), "name": metadata["name"]},
                    },
                )
                if message.attachment_kind == "image":
                    params["message"].append({"type": "image", "data": {"file": "file://" + path}})
                else:
                    action = (
                        "upload_private_file" if target.kind == "person" else "upload_group_file"
                    )
                    params = {
                        "user_id" if target.kind == "person" else "group_id": int(
                            route.external_target_id
                        ),
                        "file": path,
                        "name": metadata["name"],
                    }
                return await self._effect(
                    name,
                    args,
                    context,
                    target,
                    route,
                    action,
                    params,
                    content=(
                        f"[文件: {metadata['name']}]"
                        if message.attachment_kind == "file"
                        else message.text or f"[图片: {metadata['name']}]"
                    ),
                    ledger_segments=(
                        tuple(params["message"])
                        if message.attachment_kind == "image"
                        else ledger_segments
                    ),
                    route_target=route_target,
                    caption=message.text if message.attachment_kind == "file" else "",
                    caption_segments=(
                        [*segments, {"type": "text", "data": {"text": message.text}}]
                        if message.mentions and message.attachment_kind == "file"
                        else None
                    ),
                )
        return await self._effect(
            name,
            args,
            context,
            target,
            route,
            action,
            params,
            content=message.text if message else None,
            route_target=route_target,
        )

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
        route_target: SocialTarget | None = None,
        caption: str = "",
        caption_segments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        receipt = await self.receipts.prepare(
            source_turn_id=context.turn_id,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            action=name,
            target=target,
            payload=args,
        )
        if receipt.status is not OperationStatus.PREPARED:
            return (
                await self._file_result(receipt.operation_id)
                if caption or caption_segments
                else receipt.model_dump(mode="json")
            )
        async with self._lock:
            await self.check_target(target, sending=name.startswith("send_"))
            if route_target is not None:
                await self.check_target(route_target, sending=True)
            fresh = (
                await self.router.resolve_presence(route.presence_id)
                if name == "recall_own_message"
                else await self.send_route(route_target or target, context)
            )
            if (
                fresh.presence_id,
                fresh.binding_id,
                fresh.route_generation,
                fresh.connection.snapshot,
            ) != (
                route.presence_id,
                route.binding_id,
                route.route_generation,
                route.connection.snapshot,
            ):
                raise SocialError("route_changed")
            now = datetime.now(UTC)
            family = (
                ("poke_person",)
                if name == "poke_person"
                else ("send_private_message", "send_group_message")
            )
            if name not in {"recall_own_message", "send_file_caption"}:
                async with self.database.sessions() as session:
                    where = (
                        SocialOperationModel.action.in_(family),
                        SocialOperationModel.updated_at >= now - timedelta(seconds=60),
                        SocialOperationModel.status.in_(["executing", "succeeded", "uncertain"]),
                    )
                    total = await session.scalar(
                        select(func.count()).select_from(SocialOperationModel).where(*where)
                    )
                    per_target = await session.scalar(
                        select(func.count())
                        .select_from(SocialOperationModel)
                        .where(*where, SocialOperationModel.target_id == str(target.id))
                    )
                family_name = "poke" if name == "poke_person" else "send"
                global_limit, target_limit = (5, 1) if family_name == "poke" else (10, 3)
                if self.runtime_config is not None:
                    global_limit = int(
                        (
                            await self.runtime_config.get_effective(
                                f"social.{family_name}_global_per_minute"
                            )
                        ).value
                        or global_limit
                    )
                    target_limit = int(
                        (
                            await self.runtime_config.get_effective(
                                f"social.{family_name}_per_target_per_minute"
                            )
                        ).value
                        or target_limit
                    )
                if int(total or 0) >= global_limit or int(per_target or 0) >= target_limit:
                    return {"error": "rate_limited", "retry_after_seconds": 60, "retryable": False}
            if not await self.receipts.claim(receipt.operation_id, presence_id=route.presence_id):
                return (
                    await self._file_result(receipt.operation_id)
                    if caption or caption_segments
                    else (await self.receipts.get(receipt.operation_id)).model_dump(mode="json")
                )
        try:
            result = await self._call(route, action, params)
            reference = (
                str(result["message_id"])
                if isinstance(result, dict) and result.get("message_id") is not None
                else None
            )
            file_upload = action in {"upload_private_file", "upload_group_file"}
            if (
                file_upload
                and reference is None
                and isinstance(result, dict)
                and result.get("file_id")
            ):
                reference = "file:" + str(result["file_id"])
            if content is not None and reference is None and not file_upload:
                raise SocialError("missing_send_receipt")
            appended = None
            async with self.database.immediate_session() as session:
                if content is not None:
                    scope = (
                        ConversationScope.private(route.sender_account_id, route.external_target_id)
                        if target.kind == "person"
                        else ConversationScope.group(
                            route.sender_account_id, route.external_target_id
                        )
                    )
                    appended = await self.writer.append(
                        scope=scope,
                        platform_message_id=reference or f"social-operation:{receipt.operation_id}",
                        sender_user_id=route.sender_account_id,
                        direction="outbound",
                        content=content,
                        segments=ledger_segments
                        if ledger_segments is not None
                        else tuple(params["message"]),
                        sender_is_bot=True,
                        origin="social_tool",
                        session=session,
                    )
                await self.receipts.finish(
                    receipt.operation_id,
                    status=OperationStatus.SUCCEEDED,
                    platform_reference=reference,
                    session=session,
                )
            if appended is not None:
                self.writer.notify_committed(appended)
        except BaseException as exc:
            async with self.database.sessions() as session, session.begin():
                await self.receipts.finish(
                    receipt.operation_id,
                    status=OperationStatus.UNCERTAIN,
                    error_category=type(exc).__name__[:64],
                    session=session,
                )
            if not isinstance(exc, Exception):
                raise
        completed = await self.receipts.get(receipt.operation_id)
        if caption or caption_segments:
            if completed.status is OperationStatus.SUCCEEDED:
                caption_context = replace(
                    context, turn_id=f"social-caption:{receipt.operation_id}", call_id="caption"
                )
                try:
                    await self._effect(
                        "send_file_caption",
                        {"text": caption, "segments": caption_segments},
                        caption_context,
                        target,
                        route,
                        "send_private_msg" if target.kind == "person" else "send_group_msg",
                        {
                            "user_id" if target.kind == "person" else "group_id": int(
                                route.external_target_id
                            ),
                            "message": caption_segments
                            or [{"type": "text", "data": {"text": caption}}],
                        },
                        content=caption,
                        route_target=route_target,
                    )
                except Exception as exc:
                    # File confirmation is immutable even if caption preflight fails.
                    # A replay only reads the child receipt; it never resumes sending.
                    logging.getLogger(__name__).warning(
                        "social_caption_preflight_failed category=%s", type(exc).__name__
                    )
                    child = await self.receipts.find(
                        caption_context.turn_id, caption_context.call_id
                    )
                    if child is not None and child.status is OperationStatus.PREPARED:
                        if await self.receipts.claim(
                            child.operation_id, presence_id=route.presence_id
                        ):
                            async with self.database.sessions() as session, session.begin():
                                await self.receipts.finish(
                                    child.operation_id,
                                    status=OperationStatus.FAILED,
                                    error_category=type(exc).__name__[:64],
                                    session=session,
                                )
            return await self._file_result(receipt.operation_id)
        return completed.model_dump(mode="json")

    async def _file_result(self, operation_id: str) -> dict[str, Any]:
        file = await self.receipts.get(operation_id)
        caption = await self.receipts.find(f"social-caption:{operation_id}", "caption")
        result = file.model_dump(mode="json")
        result["file"] = file.model_dump(mode="json")
        result["caption"] = caption.model_dump(mode="json") if caption else {"status": "not_sent"}
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
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

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
        return await self._effect(
            "recall_own_message",
            args,
            context,
            target,
            route,
            "delete_msg",
            {"message_id": event.platform_message_id},
        )
