"""Asynchronous emoji candidate collection from real current-message media."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from yuki.admin.models import EmojiRuntimeConfig
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, MessageAttachment
from yuki.emoji.detector import EmojiCandidateDetector
from yuki.emoji.models import EmojiAsset, EmojiCollectionMode
from yuki.emoji.repository import EmojiRepository
from yuki.emoji.storage import EmojiStorage
from yuki.gateway.media import PlatformMediaGateway
from yuki.identity.authority import person_is_superuser
from yuki.services.media_resolver import MediaResolver
from yuki.services.plugin_events import LifecycleEventPublisher, publish_notification
from yuki.vision.models import MediaReference
from yuki_plugin_sdk.events import EventName

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmojiCollectionResult:
    collected: int
    created: int
    restored: int
    failed: int


class EmojiCollector:
    """Download exact candidates, store originals, and enqueue classification."""

    def __init__(
        self,
        *,
        detector: EmojiCandidateDetector,
        resolver: MediaResolver,
        storage: EmojiStorage,
        repository: EmojiRepository,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._detector = detector
        self._resolver = resolver
        self._storage = storage
        self._repository = repository
        self._event_publisher = event_publisher
        self._tasks: set[asyncio.Task[EmojiCollectionResult]] = set()

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    def submit(
        self,
        message: InboundMessage,
        *,
        source_event_id: int | None,
        runtime: EmojiRuntimeConfig,
        gateway: PlatformMediaGateway | None,
    ) -> None:
        """Start collection without delaying the current text-chat pipeline."""

        task = asyncio.create_task(
            self.collect_message(
                message,
                source_event_id=source_event_id,
                runtime=runtime,
                gateway=gateway,
            ),
            name=f"emoji-collect-{message.message_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._collection_finished)

    def _collection_finished(self, task: asyncio.Task[EmojiCollectionResult]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("emoji_collection_task_failed error_category=%s", type(exc).__name__)

    async def close(self) -> None:
        """Let submitted downloads finish before shared media clients are closed."""

        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def collect_message(
        self,
        message: InboundMessage,
        *,
        source_event_id: int | None,
        runtime: EmojiRuntimeConfig,
        gateway: PlatformMediaGateway | None,
    ) -> EmojiCollectionResult:
        if not runtime.enabled or not runtime.collection_enabled:
            return EmojiCollectionResult(0, 0, 0, 0)
        if message.scope_type is ScopeType.PRIVATE and not runtime.collect_private:
            return EmojiCollectionResult(0, 0, 0, 0)
        if message.scope_type is ScopeType.GROUP and not runtime.collect_group:
            return EmojiCollectionResult(0, 0, 0, 0)
        mode = EmojiCollectionMode(runtime.collection_mode)
        candidates = tuple(
            attachment
            for attachment in message.attachments
            if self._detector.is_candidate(attachment, mode)
        )
        collected = created_count = restored = failed = 0
        for attachment in candidates:
            try:
                asset, created, was_restored = await self.collect_attachment(
                    attachment,
                    message=message,
                    source_event_id=source_event_id,
                    runtime=runtime,
                    gateway=gateway,
                )
                collected += 1
                created_count += int(created)
                restored += int(was_restored)
                if created or was_restored or not asset.analysis_version:
                    await self._repository.enqueue(asset.id, "analyze")
            except (OSError, RuntimeError, ValueError) as exc:
                failed += 1
                logger.warning(
                    "emoji_collection_failed error_category=%s segment_index=%d",
                    type(exc).__name__,
                    attachment.segment_index,
                )
        return EmojiCollectionResult(collected, created_count, restored, failed)

    async def collect_attachment(
        self,
        attachment: MessageAttachment,
        *,
        message: InboundMessage,
        source_event_id: int | None,
        runtime: EmojiRuntimeConfig,
        gateway: PlatformMediaGateway | None,
        actor_person_id: str | None = None,
    ) -> tuple[EmojiAsset, bool, bool]:
        reference = MediaReference(
            message_id=message.message_id,
            segment_index=attachment.segment_index,
            source="current" if attachment.source == "current" else "reply",
            file=attachment.file,
            url=attachment.url,
            summary=attachment.summary,
            sub_type=attachment.sub_type,
            declared_size=attachment.file_size,
            emoji_id=attachment.emoji_id,
            emoji_package_id=attachment.emoji_package_id,
        )
        downloaded = (
            await attachment.prepared_media.resolve(self._resolver.max_download_bytes)
            if attachment.prepared_media is not None
            else await self._resolver.resolve(reference, gateway)
        )
        if actor_person_id is not None:
            async with self._repository._database.sessions() as session:
                if not await person_is_superuser(session, actor_person_id):
                    raise PermissionError("表情管理资格已撤销。")
        media = self._storage.inspect(
            downloaded.content,
            near_duplicate_enabled=runtime.near_duplicate_enabled,
        )
        existing = await self._repository.get_by_hash(media.sha256)
        was_missing = bool(existing and not self._storage.exists(existing.relative_path))
        self._storage.persist(downloaded.content, media)
        asset, created = await self._repository.record_candidate(
            media,
            source_event_id=source_event_id,
            actor_person_id=actor_person_id,
            user_id=message.sender.user_id,
            group_id=message.group_id,
            person_id=message.person_id,
            space_id=message.space_id,
            source_sub_type=attachment.sub_type or "",
            source_emoji_id=attachment.emoji_id or "",
            source_package_id=attachment.emoji_package_id or "",
        )
        near_duplicates: tuple[str, ...] = ()
        if runtime.near_duplicate_enabled and media.perceptual_hash is not None:
            near_duplicates = await self._repository.near_duplicates(
                media.perceptual_hash,
                max_distance=runtime.near_duplicate_distance,
                exclude_id=asset.id,
            )
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_COLLECTED,
            {
                "emoji_id": asset.id,
                "created": created,
                "restored": was_missing,
                "animated": asset.animated,
                "scope_type": message.scope_type.value,
                "near_duplicate_ids": list(near_duplicates[:20]),
            },
        )
        if was_missing:
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_RESTORED,
                {"emoji_id": asset.id},
            )
        return asset, created, was_missing
