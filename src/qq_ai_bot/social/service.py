"""Canonical social operations shared by chat and delegated automation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select

from qq_ai_bot.adapters.onebot.sender import parse_onebot_send_receipt
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
from qq_ai_bot.llm.base import LLMEmptyResponseError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.services.message_splitter import OutboundMessageSplitter
from qq_ai_bot.services.renderer import sanitize_model_output
from qq_ai_bot.social.models import (
    OperationStatus,
    SocialError,
    SocialMessage,
    SocialReceipt,
    SocialTarget,
)
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
    voice_delivery_allowed: bool = True
    inbound: Any = None
    # Backend-only proof from the current private inbound event, never tool arguments.
    reply_message_id: str | None = None
    reply_presence_id: str | None = None
    sequence_part_index: int | None = None


class SocialService:
    def __init__(
        self, database: Database, router: PresenceRouter, writer: ScopedEventLedgerUnitOfWork
    ) -> None:
        self.database = database
        self.router = router
        self.writer = writer
        self.receipts = SocialOperationRepository(database)
        self._lock = asyncio.Lock()
        self._directory_lock = asyncio.Lock()
        self._directory_checked_at = float("-inf")
        self.runtime_config: RuntimeConfigService | None = None
        self.transfer: ArtifactTransfer | None = None
        self.speech_delivery: Any = None
        self.emoji_delivery: Any = None

    @staticmethod
    def _canonical_send_arguments(args: dict[str, Any]) -> dict[str, Any]:
        """Normalize model-owned text once before receipts or transport planning."""

        canonical = dict(args)
        raw_text = canonical.get("text", "")
        if isinstance(raw_text, str):
            try:
                canonical["text"] = sanitize_model_output(
                    raw_text,
                    max_characters=12_000,
                )
            except LLMEmptyResponseError:
                canonical["text"] = ""
        if not canonical.get("text") and not any(
            canonical.get(key) for key in ("artifact_id", "emoji", "mentions")
        ):
            raise SocialError("empty_message_after_sanitization")
        SocialMessage.model_validate(
            {key: value for key, value in canonical.items() if key in SocialMessage.model_fields}
        )
        return canonical

    @staticmethod
    async def _call(route: ResolvedSend, action: str, params: dict[str, Any]) -> Any:
        provider = {"napcat": NapCatProvider(), "snowluma": SnowLumaProvider()}.get(
            route.connection.snapshot.provider
        )
        if provider is None:
            raise SocialError("capability_unavailable")
        async with asyncio.timeout(30):
            return await provider.social_action(route.connection.bot, action, params)

    async def refresh_space_names(self) -> None:
        """Refresh known bindings through live gateways, without changing grants/routes."""
        async with self._directory_lock:
            if time.monotonic() - self._directory_checked_at < 30:
                return
            self._directory_checked_at = time.monotonic()
            async with self.database.sessions() as session:
                presences = list(
                    await session.scalars(
                        select(PresenceModel.id).where(
                            PresenceModel.platform == "qq", PresenceModel.enabled.is_(True)
                        )
                    )
                )

            async def names_for(presence_id: str) -> dict[str, str]:
                try:
                    async with asyncio.timeout(3):
                        route = await self.router.resolve_presence(presence_id)
                        payload = await self._call(route, "get_group_list", {"no_cache": True})
                    if isinstance(payload, dict):
                        payload = payload.get("data", [])
                    if not isinstance(payload, list):
                        return {}
                    from qq_ai_bot.services.user_profiles import sanitize_profile_name

                    return {
                        str(item["group_id"]): sanitize_profile_name(item["group_name"])
                        for item in payload
                        if isinstance(item, dict)
                        and item.get("group_id") is not None
                        and isinstance(item.get("group_name"), str)
                        and item["group_name"].strip()
                    }
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "group_directory_refresh_failed category=%s", type(exc).__name__
                    )
                    return {}

            observed: dict[str, set[str]] = {}
            for names in await asyncio.gather(*(names_for(pid) for pid in presences)):
                for external_id, name in names.items():
                    if name:
                        observed.setdefault(external_id, set()).add(name)
            names = {
                key: next(iter(values)) for key, values in observed.items() if len(values) == 1
            }
            if not names:
                return
            now = datetime.now(UTC)
            async with self.database.sessions.begin() as session:
                rows = (
                    await session.execute(
                        select(SpaceBindingModel, CanonicalSpaceModel)
                        .join(
                            CanonicalSpaceModel,
                            CanonicalSpaceModel.id == SpaceBindingModel.space_id,
                        )
                        .where(
                            SpaceBindingModel.platform == "qq",
                            SpaceBindingModel.status == "active",
                            SpaceBindingModel.external_space_id.in_(names),
                        )
                    )
                ).all()
                # All reads are complete before staging writes. Never invent memberships.
                for binding, space in rows:
                    name = names[binding.external_space_id][:128]
                    if binding.display_name != name:
                        binding.display_name = name
                        binding.updated_at = now
                        binding.revision += 1
                    if space.name != name:
                        space.name = name
                        space.updated_at = now
                        space.revision += 1

    async def contacts(
        self, kind: str, query: str = "", *, exact: bool = False, _refresh: bool = True
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
        if kind == "space" and query and not rows and _refresh:
            await self.refresh_space_names()
            return await self.contacts(kind, query, exact=exact, _refresh=False)
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
        if (
            target.kind == "space"
            and context.trigger_event_id is not None
            and str(target.id) == context.space_id
        ):
            async with self.database.sessions() as session:
                event = await session.get(ChatEventModel, context.trigger_event_id)
                if (
                    event is None
                    or event.canonical_conversation_id != context.conversation_id
                    or event.scope_type != "group"
                    or event.direction != "inbound"
                    or event.ingress_presence_id is None
                ):
                    raise SocialError("invalid_reply_context")
                binding = await session.scalar(
                    select(SpaceBindingModel).where(
                        SpaceBindingModel.space_id == str(target.id),
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.external_space_id == event.group_id,
                        SpaceBindingModel.status == "active",
                    )
                )
                presence = await session.get(PresenceModel, event.ingress_presence_id)
                if binding is None or presence is None or not presence.enabled:
                    raise SocialError("reply_identity_unavailable")
                account, binding_id, group = (
                    presence.external_account_id,
                    binding.id,
                    binding.external_space_id,
                )
            route = await self.router.resolve_send_for_account(account, capability="send_group")
            if route.presence_id != event.ingress_presence_id:
                raise SocialError("reply_presence_changed")
            return replace(
                route,
                binding_id=binding_id,
                external_target_id=group,
                kind="group",
                route_generation=0,
            )
        if (
            target.kind != "person"
            or context.trigger_event_id is None
            or context.reply_presence_id is None
        ):
            return await self.route(target)
        async with self.database.sessions() as session:
            event = await session.get(ChatEventModel, context.trigger_event_id)
            if (
                event is None
                or event.canonical_conversation_id != context.conversation_id
                or event.ingress_presence_id != context.reply_presence_id
                or event.scope_type != "private"
                or event.direction != "inbound"
                or event.author_kind != "person"
            ):
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
            or event.bot_user_id != route.sender_account_id
            or event.scope_type != ("group" if target.kind == "space" else "private")
            or (event.group_id if target.kind == "space" else event.private_peer_user_id)
            != route.external_target_id
            or not event.platform_message_id.isdigit()
        ):
            raise SocialError("reply_event_unavailable")
        return event.platform_message_id

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

    async def _send_message_sequence(
        self, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any]:
        """Reuse the former reply layout, with one durable receipt per actual send."""
        prior = await self.receipts.find(context.turn_id, context.call_id)
        if prior is not None and prior.action == "send_message":
            # Calls created before sequence support retain their original receipt.
            return await self.execute("send_message", args, replace(context, sequence_part_index=0))
        message = SocialMessage.model_validate(
            {key: value for key, value in args.items() if key in SocialMessage.model_fields}
        )
        if not message.text or any((message.artifact_id, message.voice, message.emoji)):
            return await self.execute("send_message", args, replace(context, sequence_part_index=0))
        snapshot = context.runtime_snapshot
        if snapshot is None and self.runtime_config is not None:
            snapshot = await self.runtime_config.snapshot()
        if snapshot is None:
            return await self.execute("send_message", args, replace(context, sequence_part_index=0))
        chunks = OutboundMessageSplitter.render(
            message.text,
            runtime=snapshot,
        )
        if len(chunks) <= 1 and prior is None:
            return await self.execute("send_message", args, replace(context, sequence_part_index=0))
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
        target = prior.target if prior is not None else await self.target(kind, selected, context)
        # A content-free manifest freezes the split plan before any gateway call.
        # It remains PREPARED because it is not itself a transport effect.
        await self.receipts.prepare(
            source_turn_id=context.turn_id,
            tool_call_id=context.call_id,
            source_conversation_id=context.conversation_id,
            action="send_message_sequence",
            target=target,
            payload={"original": args, "chunks": chunks},
        )
        prefix = hashlib.sha256(context.call_id.encode()).hexdigest()[:24]
        planned = []
        for index, chunk in enumerate(chunks):
            part_args = dict(args, text=chunk)
            if index:
                part_args.pop("mentions", None)
                part_args.pop("reply_to_event_id", None)
            part_context = replace(
                context, call_id=f"seq:{prefix}:{index}", sequence_part_index=index
            )
            planned.append((part_args, part_context))
        parts: list[dict[str, Any]] = []
        for index, (part_args, part_context) in enumerate(planned):
            if index:
                delay = random.uniform(
                    snapshot.reply.delay_min_seconds, snapshot.reply.delay_max_seconds
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            part = await self.execute("send_message", part_args, part_context)
            parts.append(part)
            if part.get("status") != OperationStatus.SUCCEEDED.value:
                break
        status = (
            OperationStatus.SUCCEEDED.value
            if len(parts) == len(chunks)
            and all(part.get("status") == OperationStatus.SUCCEEDED.value for part in parts)
            else parts[-1].get("status", OperationStatus.FAILED.value)
        )
        result = {
            "status": status,
            "target": target.model_dump(mode="json"),
            "planned_messages": len(chunks),
            "sent_messages": sum(
                part.get("status") == OperationStatus.SUCCEEDED.value for part in parts
            ),
            "parts": parts,
        }
        if status != OperationStatus.SUCCEEDED.value:
            last = parts[-1]
            result["error"] = (
                last.get("error")
                or last.get("error_category")
                or (
                    "delivery_uncertain"
                    if status == OperationStatus.UNCERTAIN.value
                    else "delivery_failed"
                )
            )
            if "retry_after_seconds" in last:
                result["retry_after_seconds"] = last["retry_after_seconds"]
        return result

    async def execute(
        self, name: str, args: dict[str, Any], context: SocialContext
    ) -> dict[str, Any]:
        if name == "send_message":
            args = self._canonical_send_arguments(args)
        if name == "send_message" and context.sequence_part_index is None:
            return await self._send_message_sequence(args, context)
        if name == "read_conversation_history":
            from qq_ai_bot.social.history import read_history

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
        if name == "send_message":
            prior = await self.receipts.find(context.turn_id, context.call_id)
            if prior is not None and prior.status is not OperationStatus.PREPARED:
                # Return the immutable effect receipt even when the contact or
                # route changed after dispatch. The hash still binds the exact
                # call, target and originating conversation.
                await self.receipts.prepare(
                    source_turn_id=context.turn_id,
                    tool_call_id=context.call_id,
                    source_conversation_id=context.conversation_id,
                    action=name,
                    target=prior.target,
                    payload=args,
                )
                await self._record_work_delivery(prior)
                if args.get("attachment_kind") == "file" and (
                    args.get("text") or args.get("mentions")
                ):
                    return await self._file_result(prior.operation_id, context)
                return await self._receipt_result(prior, context)
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
        else:
            kind = "space" if name == "get_group_members" else "person"
            target_args = args
        target = await self.target(kind, target_args, context)
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
        if name not in {"send_message", "poke_person"}:
            raise SocialError("unknown_tool")
        message = (
            None
            if name == "poke_person"
            else SocialMessage.model_validate(
                {
                    key: value
                    for key, value in args.items()
                    if key
                    in {"text", "artifact_id", "attachment_kind", "mentions", "voice", "emoji"}
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
        current_group_grant = (
            name == "send_message"
            and target.kind == "space"
            and str(target.id) == context.space_id
            and (
                context.trigger_event_id is not None
                or (
                    context.origin == "plugin_background" and context.caused_by_event_id is not None
                )
            )
        )
        await self.check_target(route_target, sending=not current_group_grant)
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
                "message": (
                    [{"type": "text", "data": {"text": message.text}}] if message.text else []
                ),
            }
            if args.get("reply_to_event_id") is not None:
                if message.attachment_kind == "file":
                    raise SocialError("file_quote_unavailable")
                reference = await self.reply_reference(
                    args["reply_to_event_id"], target, route, context
                )
                params["message"].insert(0, {"type": "reply", "data": {"id": reference}})
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
                quoted = (
                    params["message"][:1]
                    if params["message"] and params["message"][0]["type"] == "reply"
                    else []
                )
                params["message"] = quoted + segments + params["message"][len(quoted) :]
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
                    await self._file_result(prior.operation_id, context)
                    if message.attachment_kind == "file" and (message.text or message.mentions)
                    else await self._receipt_result(prior, context)
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
        if message is not None and (message.voice is not None or message.emoji is not None):
            prior = await self.receipts.find(context.turn_id, context.call_id)
            if prior is not None and prior.status is not OperationStatus.PREPARED:
                return await self._effect(
                    name,
                    args,
                    context,
                    target,
                    route,
                    action,
                    params,
                    content=message.text,
                    route_target=route_target,
                )
            if context.runtime_snapshot is None:
                raise SocialError("media_context_unavailable")
            # Preparation needs the real resolved destination, not a human actor.
            # Background turns have no actor and cannot manufacture one to send media.
            media_scope = (
                ConversationScope.private(route.sender_account_id, route.external_target_id)
                if target.kind == "person"
                else ConversationScope.group(route.sender_account_id, route.external_target_id)
            )
            if message.voice is not None:
                if self.speech_delivery is None:
                    raise SocialError("speech_unavailable")
                speech_config = context.runtime_snapshot.speech
                if not speech_config.enabled or not speech_config.agent_delivery_enabled:
                    raise SocialError("speech_unavailable")
                if not context.voice_delivery_allowed:
                    raise SocialError("voice_delivery_disabled")
                from qq_ai_bot.speech.models import VoiceMode

                prepared = await self.speech_delivery.prepare(
                    scope=media_scope,
                    canonical_conversation_id=context.conversation_id,
                    response_text=message.text,
                    runtime=context.runtime_snapshot,
                    token=context.turn_token,
                    conversation_key=context.conversation_key,
                    mode=VoiceMode.VOICE,
                    style_hint=message.voice.style_hint,
                    language_hint=message.voice.language,
                )
                if prepared is None:
                    raise SocialError("speech_unavailable")
                media = prepared.message.media[0]
                if media.local_path is None:
                    raise SocialError("speech_media_unavailable")
                data = await asyncio.to_thread(Path(media.local_path).read_bytes)
                quoted = [segment for segment in params["message"] if segment["type"] == "reply"]
                params["message"] = [
                    *quoted,
                    {
                        "type": "record",
                        "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")},
                    },
                ]
                del data
                ledger_segments = (
                    *quoted,
                    {
                        "type": "record",
                        "data": {
                            "summary": media.summary,
                            "mime_type": media.mime_type,
                            "duration_milliseconds": media.duration_milliseconds,
                            "profile_id": media.voice_profile_id or "",
                            "reference_key": media.voice_reference_key or "",
                            "target_language": media.voice_language or "",
                            "generation_id": media.generation_id,
                        },
                    },
                )
                content = media.spoken_text or message.text
                outbound = prepared.message
            else:
                assert message.emoji is not None
                if self.emoji_delivery is None:
                    raise SocialError("emoji_unavailable")
                if not context.runtime_snapshot.emoji.enabled:
                    raise SocialError("emoji_unavailable")
                from qq_ai_bot.emoji.models import (
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
                    scope=media_scope,
                    response_text=message.text,
                    runtime=context.runtime_snapshot,
                )
                if (
                    prepared_emoji.status is not EmojiPreparationStatus.READY
                    or prepared_emoji.message is None
                ):
                    raise SocialError("emoji_" + prepared_emoji.reason_code)
                media = prepared_emoji.message.media[0]
                params["message"].append(
                    {
                        "type": "image",
                        "data": {
                            "file": "base64://" + base64.b64encode(media.content).decode("ascii"),
                            "sub_type": 1,
                        },
                    }
                )
                ledger_segments = (
                    *params["message"][:-1],
                    {
                        "type": "image",
                        "data": {
                            "emoji_id": media.emoji_id or "",
                            "summary": media.summary[:2000],
                            "mime_type": media.mime_type,
                            "animated": media.animated,
                        },
                    },
                )
                content = message.text
                outbound = prepared_emoji.message
            receipt = await self._effect(
                name,
                args,
                context,
                target,
                route,
                action,
                params,
                content=content,
                ledger_segments=ledger_segments,
                route_target=route_target,
            )
            if receipt.get("status") == OperationStatus.SUCCEEDED:
                try:
                    if message.voice is not None:
                        await self.speech_delivery.record_success(outbound)
                    else:
                        await self.emoji_delivery.record_send_accepted(
                            outbound, source="agent_delivery"
                        )
                        if context.inbound is not None:
                            await self.emoji_delivery.record_success(
                                outbound,
                                inbound=context.inbound,
                                source="agent",
                                ledger_recorded=True,
                            )
                except Exception:
                    # A post-send metric must never turn a durable success into
                    # an apparent failure that tempts the model to resend.
                    logging.getLogger(__name__).exception("social_media_post_send_record_failed")
            return receipt
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
            await self._record_work_delivery(receipt)
            return (
                await self._file_result(receipt.operation_id, context)
                if caption or caption_segments
                else await self._receipt_result(receipt, context)
            )
        async with self._lock:
            current_group_grant = (
                name == "send_message"
                and target.kind == "space"
                and str(target.id) == context.space_id
                and (
                    context.trigger_event_id is not None
                    or (
                        context.origin == "plugin_background"
                        and context.caused_by_event_id is not None
                    )
                )
            )
            await self.check_target(
                target, sending=name.startswith("send_") and not current_group_grant
            )
            if route_target is not None and route_target != target:
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
            from qq_ai_bot.runtime.delivery_intents import reserve
            from qq_ai_bot.runtime.work_activation import current_work_control

            work_control = current_work_control.get()
            if (
                work_control is not None
                and name.startswith("send_")
                and name != "send_file_caption"
            ):
                await reserve(
                    work_control,
                    receipt.operation_id,
                    "artifact" if caption or caption_segments else "message",
                    {"target": target.model_dump(mode="json"), "arguments": args},
                    count=2 if caption or caption_segments else 1,
                )
            if not await self.receipts.claim(receipt.operation_id, presence_id=route.presence_id):
                current = await self.receipts.get(receipt.operation_id)
                await self._record_work_delivery(current)
                return (
                    await self._file_result(receipt.operation_id, context)
                    if caption or caption_segments
                    else await self._receipt_result(current, context)
                )
        try:
            result = await self._call(route, action, params)
            reference = None
            if action in {"send_private_msg", "send_group_msg"}:
                reference = parse_onebot_send_receipt(result).platform_message_id
            file_upload = action in {"upload_private_file", "upload_group_file"}
            if file_upload and isinstance(result, dict):
                # Upload completion is independent of a retractable chat receipt.
                # Only retain genuine scalar IDs; no fabricated handles from objects.
                for field_name, prefix in (("message_id", ""), ("file_id", "file:")):
                    candidate = result.get(field_name)
                    if (
                        isinstance(candidate, (str, int))
                        and not isinstance(candidate, bool)
                        and str(candidate).strip()
                    ):
                        reference = prefix + str(candidate).strip()
                        break
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
                        origin=context.origin,
                        caused_by_event_id=context.caused_by_event_id,
                        reply_to_message_id=next(
                            (
                                str(segment["data"]["id"])
                                for segment in params.get("message", ())
                                if segment.get("type") == "reply"
                            ),
                            None,
                        ),
                        session=session,
                    )
                await self.receipts.finish(
                    receipt.operation_id,
                    status=OperationStatus.SUCCEEDED,
                    platform_reference=reference,
                    session=session,
                )
        except BaseException as exc:
            try:
                async with self.database.sessions() as session, session.begin():
                    await self.receipts.finish(
                        receipt.operation_id,
                        status=OperationStatus.UNCERTAIN,
                        error_category=type(exc).__name__[:64],
                        session=session,
                    )
            except Exception as secondary:
                exc.add_note(f"social receipt reconciliation deferred: {type(secondary).__name__}")
                raise exc from secondary
            if not isinstance(exc, Exception):
                raise
        else:
            if appended is not None:
                try:
                    self.writer.notify_committed(appended)
                except Exception:
                    # The send and ledger already committed. A wakeup failure
                    # cannot downgrade success or create a retryable delivery.
                    logging.getLogger(__name__).exception("social_post_commit_notify_failed")
        completed = await self.receipts.get(receipt.operation_id)
        await self._record_work_delivery(completed)
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
            return await self._file_result(receipt.operation_id, context)
        return await self._receipt_result(completed, context)

    @staticmethod
    async def _record_work_delivery(receipt: Any) -> None:
        from qq_ai_bot.runtime.delivery_intents import record
        from qq_ai_bot.runtime.work_activation import current_work_control

        control = current_work_control.get()
        if control is None or control.current is None:
            return
        state = "accepted" if receipt.status is OperationStatus.SUCCEEDED else "unknown"
        try:
            await record(control, receipt.operation_id, state, receipt.model_dump(mode="json"))
        except Exception:
            logging.getLogger(__name__).exception("social_work_delivery_record_failed")

    async def _receipt_result(
        self, receipt: SocialReceipt, context: SocialContext
    ) -> dict[str, Any]:
        """Project existing confirmed ledger content; never rebuild events or ownership."""
        result = receipt.model_dump(mode="json")
        if receipt.status is not OperationStatus.SUCCEEDED or receipt.action not in {
            "send_message",
            "send_file_caption",
        }:
            return result
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.social.db_models import SocialOperationModel

        try:
            async with self.database.sessions() as session:
                operation = await session.get(SocialOperationModel, receipt.operation_id)
                if operation is None:
                    return result
                events = list(
                    await session.scalars(
                        select(ChatEventModel)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id
                            == ChatEventModel.canonical_conversation_id,
                        )
                        .where(
                            ChatEventModel.platform_message_id
                            == (
                                receipt.platform_reference
                                or f"social-operation:{receipt.operation_id}"
                            ),
                            ChatEventModel.author_presence_id == receipt.presence_id,
                            ChatEventModel.direction == "outbound",
                            ChatEventModel.author_kind == "yuki",
                            ChatEventModel.event_kind == "message",
                            ChatEventModel.suppression_status == "keeper",
                            ChatEventModel.origin == context.origin,
                            ChatEventModel.caused_by_event_id == context.caused_by_event_id,
                            ChatEventModel.occurred_at >= operation.created_at,
                            ChatEventModel.occurred_at <= operation.updated_at,
                            (CanonicalConversationModel.space_id == str(receipt.target.id))
                            if receipt.target.kind == "space"
                            else (CanonicalConversationModel.person_id == str(receipt.target.id)),
                        )
                        .limit(2)
                    )
                )
            if len(events) != 1:
                return result
            event = events[0]
            segments = json.loads(event.segments_json)
            # Speech's spoken text is its visible content. File/image placeholders
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
            result.update(event_id=event.id, delivered_text=text)
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
