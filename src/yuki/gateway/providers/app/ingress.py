"""Run the original Processor independently of an HTTP connection's lifetime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    InboundMessage,
    LocalDeliveryReceipt,
    MessageAttachment,
    OutboundMessage,
    SenderIdentity,
)
from yuki.gateway.providers.app import AppHandle
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import find_presence
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.database import Database
from yuki.persistence.scoped_event_uow import ScopedAppendResult
from yuki.social.service import SocialContext, SocialService

if TYPE_CHECKING:
    from yuki.client_api.identity import ClientPrincipal
    from yuki.services.processor import MessageProcessor, ProcessResult


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
        presence_id: str,
    ) -> None:
        self.bot, self.social, self.binding = handle, social, binding
        self.conversation_id, self.request_id = conversation_id, request_id
        self.presence_id = presence_id
        self.event_id: int | None = None
        self.sequence = 0

    async def send(self, message: OutboundMessage) -> LocalDeliveryReceipt:
        self.sequence += 1
        result = await self.social.execute(
            "send_message",
            {"text": message.text},
            SocialContext(
                f"{self.binding.platform}:{self.binding.id}:"
                + (f"{self.conversation_id}:" if self.binding.platform == "acp" else "")
                + self.request_id,
                f"reply:{self.sequence}",
                self.conversation_id,
                person_refs={"current_speaker": self.binding.person_id},
                reply_presence_id=self.presence_id,
                trigger_event_id=self.event_id,
                caused_by_event_id=self.event_id,
            ),
        )
        if result["status"] != "succeeded" or result.get("event_id") is None:
            raise RuntimeError("local delivery unconfirmed")
        return LocalDeliveryReceipt(result["event_id"])


@dataclass
class _Request:
    task: asyncio.Task[Any]
    acknowledged: asyncio.Future[ScopedAppendResult]


class AppGateway:
    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        processor: MessageProcessor,
        social: SocialService,
        *,
        platform: str = "app",
    ) -> None:
        self.database, self.registry, self.processor, self.social = (
            database,
            registry,
            processor,
            social,
        )
        self.platform = platform
        self.handle = AppHandle(platform)
        self._requests: dict[tuple[str, str], _Request] = {}
        self.on_finished: (
            Callable[
                [ClientPrincipal, str, int, ProcessResult | None, BaseException | None],
                Awaitable[None],
            ]
            | None
        ) = None

    @property
    def tasks(self) -> set[asyncio.Task[Any]]:
        return {request.task for request in self._requests.values()}

    def request_task(self, conversation_id: str, request_id: str) -> asyncio.Task[Any] | None:
        request = self._requests.get((conversation_id, request_id))
        return request.task if request is not None else None

    async def start(self) -> None:
        async with self.database.sessions() as session:
            presence = await find_presence(session, self.handle.self_id, platform=self.platform)
        self.registry.connect(
            self.handle, provider_id=self.platform, presence_id=presence.id if presence else None
        )

    async def close(self) -> None:
        self.registry.disconnect(self.handle)
        tasks = tuple(self.tasks)
        for task in tasks:
            if not task.cancelling():
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
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
    ) -> InboundMessage:
        from yuki.client_api.identity import request_message_key

        return InboundMessage(
            message_id=request_message_key(
                binding.external_account_id,
                request_id,
                conversation_id=(conversation_id or thread_id) if self.platform == "acp" else None,
            ),
            event_type=f"message:{self.platform}",
            platform=self.platform,
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
            thread_id=thread_id,
        )

    async def receive(
        self,
        binding: IdentityBindingModel,
        conversation_id: str,
        request_id: str,
        text: str,
        segments: tuple[dict[str, Any], ...],
        attachments: tuple[MessageAttachment, ...] = (),
        *,
        principal: ClientPrincipal | None = None,
        thread_id: str | None = None,
    ) -> ScopedAppendResult:
        async with self.database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            assert conversation is not None and conversation.entry_presence_id is not None
            presence_id = conversation.entry_presence_id
        key = (conversation_id, request_id)
        existing = self._requests.get(key)
        if existing is not None:
            return replace(await asyncio.shield(existing.acknowledged), created=False)
        inbound = self.message(
            binding,
            request_id,
            text,
            segments,
            attachments,
            thread_id=thread_id,
            conversation_id=conversation_id,
        )
        sender = AppSender(
            self.handle, self.social, binding, conversation_id, request_id, presence_id
        )
        acknowledged: asyncio.Future[ScopedAppendResult] = (
            asyncio.get_running_loop().create_future()
        )
        acknowledged.add_done_callback(
            lambda future: future.exception() if not future.cancelled() else None
        )

        created = False

        def committed(result: ScopedAppendResult) -> None:
            nonlocal created
            created = result.created
            sender.event_id = result.event.id
            if not acknowledged.done():
                acknowledged.set_result(result)

        async def process() -> ProcessResult:
            result = None
            error = None
            try:
                result = await self.processor.handle(inbound, sender, on_committed=committed)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                if created and sender.event_id is not None and principal is not None:
                    if self.on_finished is not None:
                        settlement = asyncio.ensure_future(
                            self.on_finished(principal, request_id, sender.event_id, result, error)
                        )
                        try:
                            await asyncio.shield(settlement)
                        except asyncio.CancelledError:
                            while not settlement.done():
                                try:
                                    await asyncio.shield(settlement)
                                except asyncio.CancelledError:
                                    continue
                            settlement.result()
                            raise

        task = asyncio.create_task(process(), name=f"{self.platform}-message:{uuid4()}")
        self._requests[key] = _Request(task, acknowledged)

        def finished(completed: asyncio.Task[Any]) -> None:
            if self._requests.get(key) is not None and self._requests[key].task is completed:
                self._requests.pop(key)
            error: BaseException | None = (
                asyncio.CancelledError() if completed.cancelled() else completed.exception()
            )
            if not acknowledged.done():
                acknowledged.set_exception(error or AppMessageRejected(completed.result().reason))
            elif error is not None and not isinstance(error, asyncio.CancelledError):
                logging.getLogger(__name__).error(
                    "client_processing_failed category=%s", type(error).__name__
                )

        task.add_done_callback(finished)
        return await asyncio.shield(acknowledged)
