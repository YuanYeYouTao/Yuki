"""Run the original Processor independently of an HTTP connection's lifetime."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    InboundMessage,
    LocalDeliveryReceipt,
    MessageAttachment,
    OutboundMessage,
    SenderIdentity,
)
from qq_ai_bot.gateway.providers.app import AppHandle
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.scoped_event_uow import ScopedAppendResult
from qq_ai_bot.social.service import SocialContext, SocialService

if TYPE_CHECKING:
    from qq_ai_bot.services.processor import MessageProcessor


class AppMessageRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AppSender:
    def __init__(
        self,
        handle: AppHandle,
        social: SocialService,
        binding: IdentityBindingModel,
        conversation_id: str,
        request_id: str,
    ) -> None:
        self.bot, self.social, self.binding = handle, social, binding
        self.conversation_id, self.request_id = conversation_id, request_id
        self.event_id: int | None = None
        self.sequence = 0

    async def send(self, message: OutboundMessage) -> LocalDeliveryReceipt:
        self.sequence += 1
        result = await self.social.execute(
            "send_message",
            {"text": message.text},
            SocialContext(
                f"app:{self.binding.id}:{self.request_id}",
                f"reply:{self.sequence}",
                self.conversation_id,
                person_refs={"current_speaker": self.binding.person_id},
                reply_presence_id=self.social.router._registry.resolve_by_handle(
                    self.bot
                ).snapshot.presence_id,
                trigger_event_id=self.event_id,
                caused_by_event_id=self.event_id,
            ),
        )
        if result["status"] != "succeeded" or result.get("event_id") is None:
            raise RuntimeError("local delivery unconfirmed")
        return LocalDeliveryReceipt(result["event_id"])


class AppGateway:
    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        processor: MessageProcessor,
        social: SocialService,
    ) -> None:
        self.database, self.registry, self.processor, self.social = (
            database,
            registry,
            processor,
            social,
        )
        self.handle = AppHandle()
        self.tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        async with self.database.sessions() as session:
            presence = await session.scalar(
                select(PresenceModel).where(
                    PresenceModel.platform == "app",
                    PresenceModel.external_account_id == self.handle.self_id,
                )
            )
        if presence is None or not presence.enabled:
            raise RuntimeError("app Presence unavailable; apply migration 0121")
        self.registry.connect(self.handle, provider_id="app", presence_id=presence.id)

    async def close(self) -> None:
        self.registry.disconnect(self.handle)
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def message(
        self,
        binding: IdentityBindingModel,
        request_id: str,
        text: str,
        segments: tuple[dict[str, Any], ...],
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> InboundMessage:
        return InboundMessage(
            message_id=f"{binding.external_account_id}:{request_id}",
            event_type="message:app",
            platform="app",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(
                binding.external_account_id,
                nickname=binding.display_name,
                nickname_known=True,
            ),
            text=text,
            bot_user_id=self.handle.self_id,
            segments=segments,
            attachments=attachments,
        )

    async def receive(
        self,
        binding: IdentityBindingModel,
        conversation_id: str,
        request_id: str,
        text: str,
        segments: tuple[dict[str, Any], ...],
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> ScopedAppendResult:
        inbound = self.message(binding, request_id, text, segments, attachments)
        sender = AppSender(self.handle, self.social, binding, conversation_id, request_id)
        acknowledged: asyncio.Future[ScopedAppendResult] = (
            asyncio.get_running_loop().create_future()
        )
        acknowledged.add_done_callback(
            lambda future: future.exception() if not future.cancelled() else None
        )

        def committed(result: ScopedAppendResult) -> None:
            sender.event_id = result.event.id
            if not acknowledged.done():
                acknowledged.set_result(result)

        task = asyncio.create_task(
            self.processor.handle(inbound, sender, on_committed=committed),
            name=f"app-message:{uuid4()}",
        )
        self.tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self.tasks.discard(completed)
            error: BaseException | None = (
                asyncio.CancelledError() if completed.cancelled() else completed.exception()
            )
            if not acknowledged.done():
                acknowledged.set_exception(error or AppMessageRejected(completed.result().reason))
            elif error is not None and not isinstance(error, asyncio.CancelledError):
                logging.getLogger(__name__).error(
                    "app_processing_failed category=%s", type(error).__name__
                )

        task.add_done_callback(finished)
        return await asyncio.shield(acknowledged)
