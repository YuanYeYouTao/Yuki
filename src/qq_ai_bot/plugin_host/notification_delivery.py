"""Persistent delivery worker for Host-owned plugin notifications."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from qq_ai_bot.automation.gateway import ProactiveGatewayError
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.routing import PresenceRouter, ResolvedSend, RouteSendError
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.plugin_host.notification_repository import (
    OutboxRecord,
    PluginNotificationRepository,
)
from yuki_plugin_sdk.errors import PluginPermissionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationDeliveryReceipt:
    message_id: str
    sender_account_id: str
    external_target_id: str
    route_kind: str
    presence_id: str = ""
    binding_id: str = ""


class NotificationTransport(Protocol):
    async def send_text(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        text: str,
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
    ) -> NotificationDeliveryReceipt: ...

    async def send_media(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        local_path: Path,
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
    ) -> NotificationDeliveryReceipt: ...


class OneBotNotificationTransport:
    def __init__(
        self,
        registry: GatewayConnectionRegistry | None = None,
        *,
        router: PresenceRouter | None = None,
    ) -> None:
        self._registry = registry
        self._router = router

    async def send_text(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        text: str,
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
    ) -> NotificationDeliveryReceipt:
        return await self._send(
            bot_user_id=bot_user_id,
            target_type=target_type,
            target_id=target_id,
            message=text,
            canonical_target_person_id=canonical_target_person_id,
            canonical_target_space_id=canonical_target_space_id,
        )

    async def send_media(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        local_path: Path,
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
    ) -> NotificationDeliveryReceipt:
        content = await asyncio.to_thread(local_path.read_bytes)
        encoded = base64.b64encode(content).decode("ascii")
        del content
        try:
            return await self._send(
                bot_user_id=bot_user_id,
                target_type=target_type,
                target_id=target_id,
                message=[{"type": "image", "data": {"file": f"base64://{encoded}"}}],
                canonical_target_person_id=canonical_target_person_id,
                canonical_target_space_id=canonical_target_space_id,
            )
        finally:
            del encoded

    async def _send(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        message: object,
        canonical_target_person_id: str | None,
        canonical_target_space_id: str | None,
    ) -> NotificationDeliveryReceipt:
        resolved = await self._resolve(
            bot_user_id=bot_user_id,
            target_type=target_type,
            target_id=target_id,
            canonical_target_person_id=canonical_target_person_id,
            canonical_target_space_id=canonical_target_space_id,
        )
        bot = resolved.connection.bot
        if bot is None:
            raise ProactiveGatewayError("bot_unavailable")
        onebot_target = resolved.external_target_id if resolved.kind != "account" else target_id
        action = "send_group_msg" if target_type == "group" else "send_private_msg"
        key = "group_id" if target_type == "group" else "user_id"
        call_api = getattr(bot, "call_api", None)
        if not callable(call_api):
            raise ProactiveGatewayError("bot_unavailable")
        try:
            result = await call_api(action, **{key: onebot_target, "message": message})
        except Exception as exc:
            raise ProactiveGatewayError("onebot_transport_uncertain", uncertain=True) from exc
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        if message_id is None or not str(message_id).strip():
            raise ProactiveGatewayError("onebot_receipt_missing", uncertain=True)
        return NotificationDeliveryReceipt(
            message_id=str(message_id)[:128],
            sender_account_id=resolved.sender_account_id,
            external_target_id=onebot_target,
            route_kind=resolved.kind,
            presence_id=resolved.presence_id,
            binding_id=resolved.binding_id,
        )

    async def _resolve(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        canonical_target_person_id: str | None,
        canonical_target_space_id: str | None,
    ) -> ResolvedSend:
        if self._router is not None:
            try:
                resolved = await self._resolve_via_router(
                    bot_user_id=bot_user_id,
                    target_type=target_type,
                    target_id=target_id,
                    canonical_target_person_id=canonical_target_person_id,
                    canonical_target_space_id=canonical_target_space_id,
                )
            except RouteSendError as exc:
                raise ProactiveGatewayError(exc.category) from exc
            if resolved.kind == "person" and target_type == "group":
                raise ProactiveGatewayError("capability")
            if resolved.kind == "space" and target_type != "group":
                raise ProactiveGatewayError("capability")
            return resolved
        if self._registry is None:
            raise ProactiveGatewayError("bot_unavailable")
        try:
            resolution = self._registry.resolve_account(IDENTITY_PLATFORM, bot_user_id)
        except RegistryClosed as exc:
            raise ProactiveGatewayError("bot_unavailable") from exc
        return ResolvedSend(
            presence_id=resolution.snapshot.presence_id or "",
            binding_id="",
            platform=resolution.snapshot.platform,
            external_target_id=target_id,
            route_generation=resolution.snapshot.generation,
            connection=resolution,
            kind="account",
            sender_account_id=resolution.snapshot.external_account_id,
        )

    async def _resolve_via_router(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        canonical_target_person_id: str | None,
        canonical_target_space_id: str | None,
    ) -> ResolvedSend:
        assert self._router is not None
        has_person = bool(canonical_target_person_id)
        has_space = bool(canonical_target_space_id)
        if has_person and has_space:
            raise RouteSendError("none")
        if has_person:
            return await self._router.resolve_send_for_person(canonical_target_person_id or "")
        if has_space:
            return await self._router.resolve_send_for_space(canonical_target_space_id or "")
        return await self._router.resolve_send_for_target(
            bot_user_id=bot_user_id,
            target_type=target_type,
            target_id=target_id,
        )


class PluginNotificationOutboxWorker:
    def __init__(
        self,
        *,
        repository: PluginNotificationRepository,
        artifacts: PluginMediaArtifactStore,
        ledger: EventLedgerRepository,
        transport: NotificationTransport | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._ledger = ledger
        self._transport = transport or OneBotNotificationTransport()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="plugin-notification-outbox")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            item = await self._repository.claim_outbox()
            if item is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._deliver(item)

    async def _deliver(self, item: OutboxRecord) -> None:
        if (
            await self._repository.grant_creator(
                plugin_id=item.plugin_id,
                target_type=item.target_type,
                target_id=item.target_id,
            )
            is None
        ):
            await self._repository.finish_outbox(
                item.id, status="cancelled", error_category="target_grant_revoked"
            )
            return
        try:
            person_id, space_id = await self._canonical_delivery_target(item)
            if item.part_type == "media":
                if item.media_handle_id is None:
                    raise PluginPermanentDeliveryError("media_handle_missing")
                artifact = await self._artifacts.resolve(
                    plugin_id=item.plugin_id,
                    handle_id=item.media_handle_id,
                )
                receipt = await self._transport.send_media(
                    bot_user_id=item.bot_user_id,
                    target_type=item.target_type,
                    target_id=item.target_id,
                    local_path=artifact.local_path,
                    canonical_target_person_id=person_id,
                    canonical_target_space_id=space_id,
                )
            else:
                receipt = await self._transport.send_text(
                    bot_user_id=item.bot_user_id,
                    target_type=item.target_type,
                    target_id=item.target_id,
                    text=item.text,
                    canonical_target_person_id=person_id,
                    canonical_target_space_id=space_id,
                )
        except (PluginPermanentDeliveryError, PluginPermissionError) as exc:
            category = (
                exc.category if isinstance(exc, PluginPermanentDeliveryError) else "media_invalid"
            )
            await self._repository.finish_outbox(item.id, status="failed", error_category=category)
            return
        except ProactiveGatewayError as exc:
            if exc.uncertain:
                await self._repository.finish_outbox(
                    item.id,
                    status="uncertain",
                    error_category=exc.category,
                )
            else:
                await self._repository.retry_outbox(item.id, error_category=exc.category)
            return
        except Exception as exc:
            logger.warning(
                "notification_outbox_delivery_failed plugin_id=%s part_type=%s error_category=%s",
                item.plugin_id,
                item.part_type,
                type(exc).__name__,
            )
            await self._repository.retry_outbox(item.id, error_category=type(exc).__name__)
            return
        try:
            await self._repository.finish_outbox(
                item.id,
                status="sent",
                platform_message_id=receipt.message_id,
            )
            await self._record_outbound(item, receipt)
        except Exception as exc:
            logger.exception(
                "notification_post_send_record_failed plugin_id=%s error_category=%s",
                item.plugin_id,
                type(exc).__name__,
            )
            await self._repository.finish_outbox(
                item.id,
                status="uncertain",
                platform_message_id=receipt.message_id,
                error_category="post_send_persistence_failed",
            )
            return
        logger.info(
            "notification_outbox_delivery plugin_id=%s part_type=%s status=sent attempts=%d",
            item.plugin_id,
            item.part_type,
            item.attempts,
        )

    async def _canonical_delivery_target(self, item: OutboxRecord) -> tuple[str | None, str | None]:
        person_id = item.canonical_target_person_id
        space_id = item.canonical_target_space_id
        has_person = bool(person_id)
        has_space = bool(space_id)
        if has_person and has_space:
            raise PluginPermanentDeliveryError("canonical_target_ambiguous")
        if has_person or has_space:
            return person_id, space_id
        if await self._repository.runtime_is_complete_v2():
            raise PluginPermanentDeliveryError("canonical_target_missing")
        return None, None

    async def _record_outbound(
        self, item: OutboxRecord, receipt: NotificationDeliveryReceipt
    ) -> None:
        scope = ScopeType(item.target_type)
        source = await self._ledger.get_event(item.source_event_id)
        summary = source.content if source is not None else "插件外部事件通知"
        content = item.text if item.part_type != "media" else ""
        segments: tuple[dict[str, object], ...]
        if item.part_type == "media":
            segments = (
                {
                    "type": "image",
                    "data": {
                        "media_handle_id": item.media_handle_id,
                        "summary": summary[:500],
                    },
                },
            )
        else:
            segments = ({"type": "text", "data": {"text": content}},)
        sender = receipt.sender_account_id or item.bot_user_id
        target = receipt.external_target_id or item.target_id
        await self._ledger.append(
            bot_user_id=sender,
            platform_message_id=receipt.message_id,
            scope_type=scope,
            sender_user_id=sender,
            direction="outbound",
            content=content,
            segments=segments,
            group_id=target if scope is ScopeType.GROUP else None,
            private_peer_user_id=target if scope is ScopeType.PRIVATE else None,
            sender_is_bot=True,
            origin="plugin_background",
        )


class PluginPermanentDeliveryError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category
