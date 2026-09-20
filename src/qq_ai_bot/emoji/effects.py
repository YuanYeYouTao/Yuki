"""Resolve queued emoji intents into transport-neutral outbound media."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage, OutboundMedia, OutboundMessage
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.emoji.models import (
    EmojiLifecycleStatus,
    EmojiPreparationResult,
    EmojiPreparationStatus,
    EmojiReplyMode,
    EmojiSelectionRequest,
    PendingReplyEffect,
)
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from yuki_plugin_sdk.events import EventName

logger = logging.getLogger(__name__)


class EmojiReplyEffectService:
    def __init__(
        self,
        *,
        selector: EmojiSelector,
        repository: EmojiRepository,
        storage: EmojiStorage,
        event_publisher: LifecycleEventPublisher | None = None,
        bot_display_name: str = "Yuki",
    ) -> None:
        self._selector = selector
        self._repository = repository
        self._storage = storage
        self._event_publisher = event_publisher
        self._bot_display_name = bot_display_name

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    async def prepare(
        self,
        effect: PendingReplyEffect,
        *,
        actor: ToolActor,
        response_text: str,
        runtime: RuntimeConfigSnapshot,
    ) -> EmojiPreparationResult:
        if effect.mode is EmojiReplyMode.NONE:
            return EmojiPreparationResult(
                status=EmojiPreparationStatus.NO_CANDIDATE,
                reason_code="effect_disabled",
            )
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_QUEUED,
            {"source": effect.source, "mode": effect.mode.value},
        )
        try:
            selection = await self._selector.select(
                EmojiSelectionRequest(
                    actor_user_id=actor.user_id,
                    group_id=actor.group_id,
                    reply_text=response_text[:4000],
                    goal=effect.goal,
                    emotion=effect.emotion,
                    explicit_request=effect.explicit_request,
                    mode=effect.mode,
                    placement=effect.placement,
                ),
                runtime=runtime.emoji,
                vision_runtime=runtime.vision,
            )
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError as exc:
            return await self._failed(
                EmojiPreparationStatus.REPOSITORY_UNAVAILABLE,
                reason_code="repository_query_failed",
                source=effect.source,
                exception=exc,
                retryable=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return await self._failed(
                EmojiPreparationStatus.UNEXPECTED_FAILURE,
                reason_code="selection_failed",
                source=effect.source,
                exception=exc,
            )
        if selection.emoji_id is None:
            logger.warning(
                "emoji_reply_prepare_declined reason=%s source=%s mode=%s",
                selection.reason,
                effect.source,
                effect.mode.value,
            )
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_PREPARE_NO_CANDIDATE,
                {"source": effect.source, "reason_code": selection.reason or "no_candidate"},
            )
            return EmojiPreparationResult(
                status=EmojiPreparationStatus.NO_CANDIDATE,
                reason_code=selection.reason or "no_candidate",
            )
        try:
            asset = await self._repository.get(selection.emoji_id)
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError as exc:
            return await self._failed(
                EmojiPreparationStatus.REPOSITORY_UNAVAILABLE,
                reason_code="repository_asset_lookup_failed",
                source=effect.source,
                exception=exc,
                retryable=True,
            )
        if asset is None:
            logger.warning("emoji_reply_asset_missing_from_repository")
            return await self._failed(
                EmojiPreparationStatus.ASSET_MISSING,
                reason_code="asset_missing",
                source=effect.source,
            )
        try:
            content = self._storage.read(asset.relative_path)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as exc:
            try:
                await self._repository.set_status(asset.id, EmojiLifecycleStatus.MISSING)
            except (SQLAlchemyError, OSError, RuntimeError):
                logger.exception("emoji_missing_status_record_failed")
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_MISSING,
                {"emoji_id": asset.id},
            )
            logger.warning("emoji_reply_asset_missing_from_storage emoji_id=%s", asset.id)
            return await self._failed(
                EmojiPreparationStatus.STORAGE_MISSING,
                reason_code="storage_missing",
                source=effect.source,
                exception=exc,
            )
        summary = asset.description or f"{self._bot_display_name} 发送了一张表情图片"
        message = OutboundMessage(
            media=(
                OutboundMedia(
                    kind=AttachmentKind.IMAGE,
                    content=content,
                    mime_type=asset.mime_type,
                    summary=summary,
                    emoji_id=asset.id,
                    animated=asset.animated,
                ),
            ),
        )
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_PREPARE_READY,
            {
                "source": effect.source,
                "mode": effect.mode.value,
                "reason_code": selection.reason,
                "selected_by": selection.selected_by,
            },
        )
        logger.info(
            "emoji_prepare_ready source=%s mode=%s selected_by=%s",
            effect.source,
            effect.mode.value,
            selection.selected_by,
        )
        return EmojiPreparationResult(
            status=EmojiPreparationStatus.READY,
            message=message,
            emoji_id=asset.id,
            reason_code=selection.reason or "selected",
        )

    async def _failed(
        self,
        status: EmojiPreparationStatus,
        *,
        reason_code: str,
        source: str,
        exception: Exception | None = None,
        retryable: bool = False,
    ) -> EmojiPreparationResult:
        logger.warning(
            "emoji_prepare_failed status=%s source=%s reason_code=%s exception_category=%s",
            status.value,
            source,
            reason_code,
            type(exception).__name__ if exception is not None else "none",
        )
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_PREPARE_FAILED,
            {
                "source": source,
                "status": status.value,
                "reason_code": reason_code,
                "exception_category": (
                    type(exception).__name__ if exception is not None else "none"
                ),
            },
        )
        return EmojiPreparationResult(
            status=status,
            reason_code=reason_code,
            retryable=retryable,
        )

    async def record_success(
        self,
        message: OutboundMessage,
        *,
        inbound: InboundMessage,
        source: str,
        ledger_recorded: bool,
    ) -> None:
        for media in message.media:
            if media.emoji_id:
                usage_recorded = False
                try:
                    await self._repository.mark_used(
                        media.emoji_id,
                        actor_user_id=inbound.sender.user_id,
                        group_id=inbound.group_id,
                        trigger_message_id=inbound.message_id,
                        source=source,
                    )
                    usage_recorded = True
                    await publish_notification(
                        self._event_publisher,
                        EventName.EMOJI_USAGE_RECORDED,
                        {"source": source, "scope_type": inbound.scope_type.value},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "emoji_usage_record_failed exception_category=%s",
                        type(exc).__name__,
                    )
                    await publish_notification(
                        self._event_publisher,
                        EventName.EMOJI_USAGE_RECORD_FAILED,
                        {"source": source, "exception_category": type(exc).__name__},
                    )
                await publish_notification(
                    self._event_publisher,
                    EventName.EMOJI_SENT,
                    {
                        "scope_type": inbound.scope_type.value,
                        "source": source,
                        "delivered": True,
                        "recorded": ledger_recorded and usage_recorded,
                    },
                )

    async def record_send_attempted(self, message: OutboundMessage, *, source: str) -> None:
        if any(media.emoji_id for media in message.media):
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_SEND_ATTEMPTED,
                {"source": source},
            )

    async def record_send_accepted(self, message: OutboundMessage, *, source: str) -> None:
        if any(media.emoji_id for media in message.media):
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_SEND_ACCEPTED,
                {"source": source},
            )

    async def record_failure(self, message: OutboundMessage, *, source: str) -> None:
        for media in message.media:
            if media.emoji_id:
                await publish_notification(
                    self._event_publisher,
                    EventName.EMOJI_SEND_FAILED,
                    {"emoji_id": media.emoji_id, "source": source},
                )
