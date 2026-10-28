"""Persistent delivery worker for Host-owned plugin notifications."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.gateway import ProactiveGatewayError
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.gateway.social import (
    TransportMedia,
    TransportMessage,
    TransportTarget,
    prepared_transport,
    transport_payload,
)
from yuki.identity.routing import PresenceRouter, ResolvedSend, RouteSendError
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.persistence.worker_recovery import recover_database_loop
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_repository import (
    BackgroundTurnFenceError,
    OutboxRecord,
    PluginNotificationRepository,
    queued_work_error_category,
)
from yuki.plugin_host.ownership import PluginOwnershipError
from yuki.services.effect_gate import (
    ConversationEffectGate,
    EffectGateTimeoutError,
)
from yuki.services.media_resolver import _media_io
from yuki.workspace.store import WorkspaceError, WorkspaceStore
from yuki_plugin_sdk.errors import PluginPermissionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationDeliveryReceipt:
    message_id: str | None
    sender_account_id: str
    external_target_id: str
    route_kind: str
    presence_id: str = ""
    binding_id: str = ""
    platform: str = "qq"
    conversation_id: str | None = None
    thread_id: str | None = None


class NotificationTransport(Protocol):
    async def resolve(self, item: OutboxRecord) -> ResolvedSend: ...

    async def validate_local(
        self, item: OutboxRecord, route: ResolvedSend, *, session: AsyncSession
    ) -> None: ...

    async def prepare(
        self, item: OutboxRecord, message: TransportMessage
    ) -> tuple[dict[str, Any], ...]: ...

    async def send_text(
        self,
        *,
        target_type: str,
        text: str,
        prepared: dict[str, Any],
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
        canonical_conversation_id: str | None = None,
        canonical_presence_id: str | None = None,
        source_event_id: int | None = None,
    ) -> NotificationDeliveryReceipt: ...

    async def send_media(
        self,
        *,
        target_type: str,
        local_path: Path,
        content_type: str,
        content: bytes,
        prepared: dict[str, Any],
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
        canonical_conversation_id: str | None = None,
        canonical_presence_id: str | None = None,
        source_event_id: int | None = None,
    ) -> NotificationDeliveryReceipt: ...


class PlatformNotificationTransport:
    """Send the existing outbox effect through the same platform Social adapter."""

    def __init__(
        self,
        *,
        database: Database,
        router: PresenceRouter | None = None,
    ) -> None:
        self._router = router
        self._database = database

    async def prepare(
        self, item: OutboxRecord, message: TransportMessage
    ) -> tuple[dict[str, Any], ...]:
        from yuki.social.content import prepare_for_entry

        if self._router is None or item.canonical_conversation_id is None:
            raise ProactiveGatewayError("none")
        try:
            route = await self._router.resolve_send_for_conversation(
                item.canonical_conversation_id,
                presence_id=item.canonical_presence_id,
                source_event_id=item.source_event_id,
            )
        except RouteSendError as exc:
            raise ProactiveGatewayError(exc.category) from exc
        operations = self._router.operations(route)
        if item.prepared is not None:
            message = prepared_transport(item.prepared["content"], message.media)
        else:
            async with self._database.read_session() as session:
                content = await prepare_for_entry(
                    session,
                    message.text,
                    platform=route.platform,
                    space_binding_id=route.binding_id if item.target_type == "group" else None,
                    participant_assignment_ids=(route.binding_id, route.presence_id),
                    conversation_id=item.canonical_conversation_id,
                    operations=operations,
                )
            message = TransportMessage(
                content.message.text,
                content.message.mentions,
                message.reply_reference,
                message.media,
            )
        provenance = {
            "presence_id": route.presence_id,
            "binding_id": route.binding_id,
            "conversation_id": item.canonical_conversation_id,
            "platform": route.platform,
            "provider_id": route.connection.snapshot.provider,
            "local_ledger": "local_ledger" in route.connection.snapshot.capabilities,
        }
        return tuple(
            {"message": transport_payload(part), "route": provenance}
            for part in operations.plan_send(message)
        )

    async def resolve(self, item: OutboxRecord) -> ResolvedSend:
        if self._router is None or item.canonical_conversation_id is None:
            raise ProactiveGatewayError("none")
        try:
            resolved = await self._router.resolve_send_for_conversation(
                item.canonical_conversation_id,
                presence_id=item.canonical_presence_id,
                source_event_id=item.source_event_id,
            )
        except RouteSendError as exc:
            raise ProactiveGatewayError(exc.category) from exc
        assert item.prepared is not None
        self._validate_route(item.prepared["route"], resolved)
        return resolved

    async def validate_local(
        self, item: OutboxRecord, route: ResolvedSend, *, session: AsyncSession
    ) -> None:
        assert self._router is not None
        target_id = item.canonical_target_person_id or item.canonical_target_space_id
        assert target_id is not None
        await self._router.validate_prepared_send(
            session,
            route,
            target_kind="person" if item.target_type == "private" else "space",
            target_id=target_id,
        )

    @staticmethod
    def _validate_route(original: dict[str, Any], resolved: ResolvedSend) -> None:
        local = "local_ledger" in resolved.connection.snapshot.capabilities
        if local and not (original.get("local_ledger") is True and original.get("provider_id")):
            raise PluginPermanentDeliveryError("local_delivery_proof_missing")
        current = {
            "presence_id": resolved.presence_id,
            "binding_id": resolved.binding_id,
            "conversation_id": resolved.conversation_id,
            "platform": resolved.platform,
            "provider_id": resolved.connection.snapshot.provider,
            "local_ledger": local,
        }
        if any(original[key] != value for key, value in current.items() if key in original):
            raise PluginPermanentDeliveryError("delivery_route_changed")

    async def send_text(
        self,
        *,
        target_type: str,
        text: str,
        prepared: dict[str, Any],
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
        canonical_conversation_id: str | None = None,
        canonical_presence_id: str | None = None,
        source_event_id: int | None = None,
    ) -> NotificationDeliveryReceipt:
        return await self._send(
            target_type=target_type,
            prepared=prepared,
            message=prepared_transport(prepared["message"]),
            canonical_target_person_id=canonical_target_person_id,
            canonical_target_space_id=canonical_target_space_id,
            canonical_conversation_id=canonical_conversation_id,
            canonical_presence_id=canonical_presence_id,
            source_event_id=source_event_id,
        )

    async def send_media(
        self,
        *,
        target_type: str,
        local_path: Path,
        content_type: str,
        content: bytes,
        prepared: dict[str, Any],
        canonical_target_person_id: str | None = None,
        canonical_target_space_id: str | None = None,
        canonical_conversation_id: str | None = None,
        canonical_presence_id: str | None = None,
        source_event_id: int | None = None,
    ) -> NotificationDeliveryReceipt:
        return await self._send(
            target_type=target_type,
            prepared=prepared,
            message=prepared_transport(
                prepared["message"],
                TransportMedia(
                    kind="image",
                    content=content,
                    filename=local_path.name,
                    mime_type=content_type,
                ),
            ),
            canonical_target_person_id=canonical_target_person_id,
            canonical_target_space_id=canonical_target_space_id,
            canonical_conversation_id=canonical_conversation_id,
            canonical_presence_id=canonical_presence_id,
            source_event_id=source_event_id,
        )

    async def _send(
        self,
        *,
        target_type: str,
        message: TransportMessage,
        prepared: dict[str, Any],
        canonical_target_person_id: str | None,
        canonical_target_space_id: str | None,
        canonical_conversation_id: str | None,
        canonical_presence_id: str | None,
        source_event_id: int | None,
    ) -> NotificationDeliveryReceipt:
        if self._router is None:
            raise ProactiveGatewayError("none")
        try:
            if canonical_conversation_id is not None:
                resolved = await self._router.resolve_send_for_conversation(
                    canonical_conversation_id,
                    presence_id=canonical_presence_id,
                    source_event_id=source_event_id,
                )
            else:
                raise RouteSendError("none")
        except RouteSendError as exc:
            category = (
                "gateway_disconnected"
                if exc.category in {"disconnected", "original_presence_unavailable"}
                else exc.category
            )
            raise ProactiveGatewayError(category) from exc
        self._validate_route(prepared["route"], resolved)
        operations = self._router.operations(resolved)
        try:
            result = await operations.send(
                resolved.connection.bot,
                TransportTarget(
                    kind="space" if target_type == "group" else "person",
                    external_id=resolved.external_target_id,
                    thread_id=resolved.thread_id,
                ),
                message,
            )
        except Exception as exc:
            raise ProactiveGatewayError(
                "platform_transport_error", uncertain=getattr(exc, "uncertain", True)
            ) from exc
        return NotificationDeliveryReceipt(
            message_id=result.reference,
            sender_account_id=resolved.sender_account_id,
            external_target_id=resolved.external_target_id,
            route_kind=resolved.kind,
            presence_id=resolved.presence_id,
            binding_id=resolved.binding_id,
            platform=resolved.platform,
            conversation_id=resolved.conversation_id,
            thread_id=resolved.thread_id,
        )


class PluginNotificationOutboxWorker:
    def __init__(
        self,
        *,
        repository: PluginNotificationRepository,
        artifacts: PluginMediaArtifactStore,
        ledger: EventLedgerRepository,
        transport: NotificationTransport,
        effect_gate: ConversationEffectGate,
        workspace_store: WorkspaceStore,
        effect_gate_timeout_seconds: float = 5.0,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._workspace = workspace_store
        self._ledger = ledger
        self._transport = transport
        self._effect_gate = effect_gate
        self._effect_gate_timeout_seconds = effect_gate_timeout_seconds
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
        await recover_database_loop(self._run_queue, stop=self._stop, logger=logger)

    async def _run_queue(self) -> None:
        while not self._stop.is_set():
            item = await self._repository.claim_outbox()
            if item is None:
                await self._prepare_pending()
                item = await self._repository.claim_outbox()
            if item is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._deliver(item)

    async def _prepare_pending(self) -> None:
        cursor = 0
        while items := await self._repository.unprepared_outbox(after_id=cursor):
            for item in items:
                try:
                    await self._repository.require_unclaimed_outbox_source(item)
                    media = None
                    if item.part_type == "media" and item.media_handle_id is None:
                        raise PluginPermanentDeliveryError("media_handle_missing")
                    if item.media_handle_id is not None:
                        artifact = await self._artifacts.resolve(
                            plugin_id=item.plugin_id, handle_id=item.media_handle_id
                        )
                        media = TransportMedia(
                            "image", b"", artifact.local_path.name, artifact.content_type
                        )
                    parts = await self._transport.prepare(
                        item, TransportMessage(item.text, media=media)
                    )
                    await self._repository.prepare_outbox(item, parts)
                    return
                except ProactiveGatewayError:
                    continue
                except (
                    PluginOwnershipError,
                    PluginPermissionError,
                    PluginPermanentDeliveryError,
                ) as exc:
                    category = (
                        queued_work_error_category(exc, item)
                        if isinstance(exc, PluginOwnershipError)
                        else exc.category
                        if isinstance(exc, PluginPermanentDeliveryError)
                        else "media_invalid"
                    )
                    await self._repository.fail_unclaimed_outbox(item, category)
            cursor = items[-1].id

    async def _deliver(self, item: OutboxRecord) -> None:
        try:
            await self._repository.require_outbox_ready(item)
        except BackgroundTurnFenceError:
            return
        except PluginOwnershipError as exc:
            await self._repository.finish_outbox(
                item.id,
                attempt=item.attempts,
                status="failed",
                error_category=queued_work_error_category(exc, item),
            )
            return
        if (
            await self._repository.granted_canonical_creator(
                plugin_id=item.plugin_id,
                person_id=item.canonical_target_person_id,
                space_id=item.canonical_target_space_id,
                conversation_id=item.canonical_conversation_id,
            )
            is None
        ):
            await self._repository.finish_outbox(
                item.id,
                attempt=item.attempts,
                status="cancelled",
                error_category="target_grant_revoked",
            )
            return
        try:
            person_id, space_id = await self._canonical_delivery_target(item)
        except PluginPermanentDeliveryError as exc:
            await self._repository.finish_outbox(
                item.id,
                attempt=item.attempts,
                status="failed",
                error_category=exc.category,
            )
            return
        if item.part_type != "agent_reply":
            await self._deliver_ready(item, person_id=person_id, space_id=space_id)
            return
        try:
            scope_key = await self._repository.require_agent_reply_ready(item)
            async with self._effect_gate.hold(
                scope_key,
                timeout_seconds=self._effect_gate_timeout_seconds,
            ):
                await self._repository.require_agent_reply_ready(item)
                await self._deliver_ready(item, person_id=person_id, space_id=space_id)
        except BackgroundTurnFenceError:
            return
        except EffectGateTimeoutError:
            await self._repository.retry_outbox(
                item.id,
                attempt=item.attempts,
                error_category="effect_gate_timeout",
            )

    async def _deliver_ready(
        self,
        item: OutboxRecord,
        *,
        person_id: str | None,
        space_id: str | None,
    ) -> None:
        assert item.prepared is not None
        stable_media = None
        local = item.prepared.get("route", {}).get("local_ledger") is True
        try:
            resolved = await self._transport.resolve(item) if local else None
            if item.part_type == "media":
                if item.media_handle_id is None:
                    raise PluginPermanentDeliveryError("media_handle_missing")
                artifact, verified = await self._artifacts.read_owned(
                    plugin_id=item.plugin_id,
                    handle_id=item.media_handle_id,
                )
                stable_media = await _media_io(
                    self._publish_media, item, artifact.filename, artifact.sha256, verified
                )
                stable_media["mime_type"] = artifact.content_type
                if resolved is not None:
                    receipt = self._local_receipt(resolved)
                else:
                    receipt = await self._transport.send_media(
                        target_type=item.target_type,
                        local_path=artifact.local_path,
                        content_type=artifact.content_type,
                        content=verified,
                        prepared=item.prepared,
                        canonical_target_person_id=person_id,
                        canonical_target_space_id=space_id,
                        canonical_conversation_id=item.canonical_conversation_id,
                        canonical_presence_id=item.canonical_presence_id,
                        source_event_id=item.source_event_id,
                    )
            elif resolved is not None:
                receipt = self._local_receipt(resolved)
            else:
                receipt = await self._transport.send_text(
                    target_type=item.target_type,
                    text=item.text,
                    prepared=item.prepared,
                    canonical_target_person_id=person_id,
                    canonical_target_space_id=space_id,
                    canonical_conversation_id=item.canonical_conversation_id,
                    canonical_presence_id=item.canonical_presence_id,
                    source_event_id=item.source_event_id,
                )
        except (PluginPermanentDeliveryError, PluginPermissionError, WorkspaceError) as exc:
            category = (
                exc.category if isinstance(exc, PluginPermanentDeliveryError) else "media_invalid"
            )
            await self._repository.finish_outbox(
                item.id,
                attempt=item.attempts,
                status="failed",
                error_category=category,
            )
            return
        except ProactiveGatewayError as exc:
            if exc.uncertain:
                await self._repository.finish_outbox(
                    item.id,
                    attempt=item.attempts,
                    status="uncertain",
                    error_category=exc.category,
                )
            elif exc.category in {"bot_unavailable", "gateway_disconnected", "effect_gate_timeout"}:
                await self._repository.retry_outbox(
                    item.id,
                    attempt=item.attempts,
                    error_category=exc.category,
                )
            else:
                await self._repository.finish_outbox(
                    item.id,
                    attempt=item.attempts,
                    status="failed",
                    error_category=exc.category,
                )
            return
        except asyncio.CancelledError:
            if local:
                await self._repository.recover_local_outbox(item)
            raise
        except Exception as exc:
            if local:
                await self._repository.recover_local_outbox(item)
                raise
            logger.warning(
                "notification_outbox_delivery_failed plugin_id=%s part_type=%s error_category=%s",
                item.plugin_id,
                item.part_type,
                type(exc).__name__,
            )
            await self._repository.finish_outbox(
                item.id,
                attempt=item.attempts,
                status="uncertain",
                error_category=type(exc).__name__,
            )
            return
        try:
            completed = await self._record_outbound(
                item, receipt, stable_media=stable_media, local_route=resolved
            )
            if not completed:
                if local:
                    await self._repository.recover_local_outbox(item)
                    return
                logger.error(
                    "notification_post_send_attempt_reclaimed plugin_id=%s part_type=%s",
                    item.plugin_id,
                    item.part_type,
                )
                return
        except (Exception, asyncio.CancelledError) as exc:
            if local:
                await self._repository.recover_local_outbox(item)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.exception("notification_local_record_failed outbox_id=%s", item.id)
                return
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.exception(
                "notification_post_send_record_failed plugin_id=%s error_category=%s",
                item.plugin_id,
                type(exc).__name__,
            )
            await self._repository.finish_outbox(
                item.id,
                attempt=item.attempts,
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

    def _publish_media(
        self, item: OutboxRecord, name: str, digest: str, content: bytes
    ) -> dict[str, Any]:
        source = f"plugin-notification:{item.id}"
        identity = str(uuid5(NAMESPACE_URL, source))
        with TemporaryFile() as stream:
            stream.write(content)
            stream.flush()
            return self._workspace.snapshot(
                stream.fileno(),
                name,
                artifact_id=identity,
                source_key=source,
                expected_version=digest,
            )

    @staticmethod
    def _local_receipt(route: ResolvedSend) -> NotificationDeliveryReceipt:
        return NotificationDeliveryReceipt(
            message_id=None,
            sender_account_id=route.sender_account_id,
            external_target_id=route.external_target_id,
            route_kind=route.kind,
            presence_id=route.presence_id,
            binding_id=route.binding_id,
            platform=route.platform,
            conversation_id=route.conversation_id,
            thread_id=route.thread_id,
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
        raise PluginPermanentDeliveryError("canonical_target_missing")

    async def _record_outbound(
        self,
        item: OutboxRecord,
        receipt: NotificationDeliveryReceipt,
        *,
        stable_media: dict[str, Any] | None = None,
        local_route: ResolvedSend | None = None,
    ) -> bool:
        scope = ScopeType(item.target_type)
        source = await self._ledger.get_event(item.source_event_id)
        summary = source.content if source is not None else "插件外部事件通知"
        assert item.prepared is not None
        actual_message = prepared_transport(item.prepared["message"])
        content = actual_message.text
        segments: tuple[dict[str, object], ...]
        if item.part_type == "media":
            assert stable_media is not None
            segments = (
                {
                    "type": "image",
                    "data": {
                        "artifact_id": stable_media["artifact_id"],
                        "version": stable_media["sha256"],
                        "filename": stable_media["name"],
                        "size": stable_media["size"],
                        "mime_type": stable_media["mime_type"],
                        "summary": summary[:500],
                    },
                },
            )
        else:
            segments = ({"type": "text", "data": {"text": content}},)
        segments += tuple(
            {
                "type": "mention",
                "data": {
                    **asdict(mention),
                    "source": {
                        "platform": receipt.platform,
                        "external_account_id": mention.external_id,
                    },
                },
            }
            for mention in actual_message.mentions
        )
        sender = receipt.sender_account_id
        target = receipt.external_target_id
        if not sender or not target or not receipt.presence_id:
            raise PluginPermanentDeliveryError("none")
        identity = (
            ConversationScope.group(
                sender,
                target,
                platform=receipt.platform,
                presence_id=receipt.presence_id,
                conversation_id=item.canonical_conversation_id,
                thread_id=receipt.thread_id,
            )
            if scope is ScopeType.GROUP
            else ConversationScope.private(
                sender,
                target,
                platform=receipt.platform,
                presence_id=receipt.presence_id,
                conversation_id=item.canonical_conversation_id,
            )
        )
        from yuki.persistence.scoped_event_uow import prepare_append

        prepared_append = await prepare_append(content, segments=segments)
        local = item.prepared.get("route", {}).get("local_ledger") is True
        native_id = receipt.message_id or None
        async with self._ledger._database.immediate_session() as session:
            completed = (
                await self._repository.settle_local_outbox(item, session=session)
                if local
                else await self._repository.finish_outbox(
                    item.id,
                    attempt=item.attempts,
                    status="sent",
                    platform_message_id=native_id,
                    session=session,
                )
            )
            if not completed:
                return False
            if local:
                assert local_route is not None
                await self._transport.validate_local(item, local_route, session=session)
            appended = await ScopedEventLedgerUnitOfWork(self._ledger._database).append(
                scope=identity,
                origin_key=native_id,
                native_message_id=native_id,
                sender_user_id=sender,
                direction="outbound",
                content=content,
                segments=segments,
                prepared=prepared_append,
                origin="plugin_background",
                caused_by_event_id=item.source_event_id,
                space_binding_id=receipt.binding_id if scope is ScopeType.GROUP else None,
                session=session,
            )
            event = appended.event
            if event.ingress_presence_id != receipt.presence_id or (
                item.canonical_conversation_id
                and event.canonical_conversation_id != item.canonical_conversation_id
            ):
                raise PluginPermanentDeliveryError("canonical_owner_mismatch")
            from yuki.plugin_host.db_models import PluginNotificationOutboxModel

            row = await session.get(PluginNotificationOutboxModel, item.id)
            if row is not None:
                row.event_id = event.id
        return True


class PluginPermanentDeliveryError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category
