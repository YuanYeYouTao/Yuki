"""Select, resolve, cache, and analyze trusted image message segments."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from yuki.admin.models import VisionRuntimeConfig
from yuki.domain.messages import AttachmentKind, InboundMessage, MessageAttachment
from yuki.gateway.media import PlatformMediaGateway
from yuki.persistence.repositories import (
    MediaAnalysisRepository,
)
from yuki.services.image_preprocessor import (
    ImagePreprocessingError,
    ImagePreprocessor,
)
from yuki.services.media_resolver import (
    MediaResolutionError,
    MediaResolver,
)
from yuki.services.vision_rate_limit import VisionRateLimiter
from yuki.vision.base import VisionError, VisionProvider
from yuki.vision.models import (
    MediaReference,
    PreparedVisualInput,
    VisionAnalysisMode,
    VisionAnalysisOptions,
    VisualObservation,
)

logger = logging.getLogger(__name__)

DEFAULT_VISUAL_QUESTION = (
    "请描述图片主要内容并尝试辨认其中的动漫、游戏、虚拟人物、吉祥物或网络表情角色；"
    "若是表情包，说明角色、情绪和常见使用语境；若包含文字，提取清晰可见的文字。"
)
VISION_PROMPT_VERSION = "vision-observation-v4"


class VisionProcessingError(RuntimeError):
    """Sanitized orchestration error used by the message pipeline."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class VisionService:
    """Run at most one provider request for one explicitly handled message."""

    def __init__(
        self,
        *,
        provider: VisionProvider,
        resolver: MediaResolver,
        preprocessor: ImagePreprocessor,
        analyses: MediaAnalysisRepository,
        rate_limiter: VisionRateLimiter,
        max_prepared_bytes: int = 6_291_456,
        global_concurrency: int = 2,
        queue_max_pending: int = 32,
        queue_timeout_seconds: float = 120.0,
        prompt_version: str = VISION_PROMPT_VERSION,
    ) -> None:
        if (
            max_prepared_bytes <= 0
            or global_concurrency <= 0
            or queue_max_pending <= 0
            or queue_timeout_seconds <= 0
        ):
            raise ValueError("vision service numeric limits must be positive")
        if not prompt_version:
            raise ValueError("prompt_version must not be empty")
        self._provider = provider
        self._resolver = resolver
        self._preprocessor = preprocessor
        self._analyses = analyses
        self._rate_limiter = rate_limiter
        self._max_prepared_bytes = max_prepared_bytes
        self._prompt_version = prompt_version[:64]
        self._pipeline_semaphore = asyncio.Semaphore(global_concurrency)
        self._queue_max_pending = queue_max_pending
        self._queue_timeout_seconds = queue_timeout_seconds
        self._active_analyses = 0
        self._queued_analyses = 0
        self._running_analyses = 0
        self._singleflight_guard = asyncio.Lock()
        self._singleflight: dict[str, asyncio.Future[VisualObservation]] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    @property
    def busy(self) -> bool:
        """Return whether at least one visual request is queued or running."""

        return self._active_analyses > 0

    @property
    def queue_depth(self) -> int:
        """Return the number of requests currently waiting for a pipeline slot."""

        return self._queued_analyses

    @property
    def running_count(self) -> int:
        """Return the number of requests currently inside the visual pipeline."""

        return self._running_analyses

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    @staticmethod
    def has_visual_input(message: InboundMessage) -> bool:
        """Return whether current or replied message contains a real image segment."""

        return any(
            attachment.kind is AttachmentKind.IMAGE
            for attachment in (*message.attachments, *message.reply_attachments)
        )

    @staticmethod
    def select_references(
        message: InboundMessage,
        *,
        maximum: int,
    ) -> tuple[MediaReference, ...]:
        """Prefer current images, otherwise reply images, preserving segment order."""

        if maximum <= 0:
            return ()
        current = tuple(
            attachment
            for attachment in message.attachments
            if attachment.kind is AttachmentKind.IMAGE
        )
        reply = tuple(
            attachment
            for attachment in message.reply_attachments
            if attachment.kind is AttachmentKind.IMAGE
        )
        selected = (current or reply)[:maximum]
        return tuple(
            _media_reference(
                attachment,
                message_id=(
                    message.message_id
                    if attachment.source == "current"
                    else message.reply_to_message_id
                ),
            )
            for attachment in selected
        )

    async def analyze(
        self,
        message: InboundMessage,
        *,
        question: str,
        runtime: VisionRuntimeConfig,
        gateway: PlatformMediaGateway | None,
        source_event_id: int | None,
        conversation_key: str,
    ) -> VisualObservation:
        """Return one cached or newly generated structured visual observation."""

        if self._closed:
            raise VisionProcessingError("closed", "视觉服务正在关闭")
        if self._queued_analyses >= self._queue_max_pending:
            logger.warning(
                "vision_queue_rejected conversation_hash=%s reason=queue_full "
                "queue_depth=%d running=%d max_pending=%d",
                _conversation_hash(conversation_key),
                self._queued_analyses,
                self._running_analyses,
                self._queue_max_pending,
            )
            raise VisionProcessingError("queue_full", "图片理解排队已满，请稍后再试")
        self._active_analyses += 1
        self._queued_analyses += 1
        self._idle.clear()
        queue_started = time.perf_counter()
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._pipeline_semaphore.acquire(),
                    timeout=self._queue_timeout_seconds,
                )
            except TimeoutError as exc:
                logger.warning(
                    "vision_queue_rejected conversation_hash=%s reason=queue_timeout "
                    "queue_wait_seconds=%.3f queue_depth=%d running=%d",
                    _conversation_hash(conversation_key),
                    time.perf_counter() - queue_started,
                    max(0, self._queued_analyses - 1),
                    self._running_analyses,
                )
                raise VisionProcessingError(
                    "queue_timeout", "图片理解排队等待超时，请稍后再试"
                ) from exc
            acquired = True
            self._queued_analyses -= 1
            self._running_analyses += 1
            logger.info(
                "vision_queue_acquired conversation_hash=%s queue_wait_seconds=%.3f "
                "queue_depth=%d running=%d",
                _conversation_hash(conversation_key),
                time.perf_counter() - queue_started,
                self._queued_analyses,
                self._running_analyses,
            )
            return await self._analyze(
                message,
                question=question,
                runtime=runtime,
                gateway=gateway,
                source_event_id=source_event_id,
                conversation_key=conversation_key,
            )
        finally:
            if acquired:
                self._running_analyses -= 1
                self._pipeline_semaphore.release()
            else:
                self._queued_analyses -= 1
            self._active_analyses -= 1
            if self._active_analyses == 0:
                self._idle.set()

    async def _analyze(
        self,
        message: InboundMessage,
        *,
        question: str,
        runtime: VisionRuntimeConfig,
        gateway: PlatformMediaGateway | None,
        source_event_id: int | None,
        conversation_key: str,
    ) -> VisualObservation:
        references = self.select_references(message, maximum=runtime.max_images_per_turn)
        if not references:
            raise VisionProcessingError("no_images", "当前消息没有可分析的图片")

        normalized_question = " ".join(question.split())[:2000]
        effective_question = normalized_question or DEFAULT_VISUAL_QUESTION
        mode = _analysis_mode(normalized_question, references)
        cache_mode = "question" if mode == "character" else mode
        cache_prompt_version = _cache_prompt_version(
            self._prompt_version,
            runtime,
            references,
        )
        question_hash = (
            hashlib.sha256(normalized_question.encode("utf-8")).hexdigest()
            if normalized_question
            else ""
        )
        started = time.perf_counter()
        prepared: list[PreparedVisualInput] = []
        prepared_references: list[MediaReference] = []
        total_bytes = 0
        prepared_bytes = 0
        remaining_frames = runtime.max_frames_per_turn
        partial_failure = False
        last_error: VisionProcessingError | None = None
        for reference in references:
            if remaining_frames <= 0:
                partial_failure = True
                break
            try:
                downloaded = await self._resolver.resolve(reference, gateway)
                total_bytes += downloaded.byte_size
                visual_input = await asyncio.to_thread(
                    self._preprocessor.prepare,
                    downloaded,
                    source=reference.source,
                    summary_hint=reference.summary,
                    max_frames=min(runtime.gif_max_frames, remaining_frames),
                )
                item_prepared_bytes = sum(
                    _data_url_size(frame.data_url) for frame in visual_input.frames
                )
                if prepared_bytes + item_prepared_bytes > self._max_prepared_bytes:
                    raise VisionProcessingError(
                        "prepared_too_large",
                        "本轮预处理后的图片总量超过限制",
                    )
                prepared.append(visual_input)
                prepared_references.append(reference)
                prepared_bytes += item_prepared_bytes
                remaining_frames -= len(visual_input.frames)
            except asyncio.CancelledError:
                raise
            except MediaResolutionError as exc:
                partial_failure = True
                error_code = "media_download_timeout" if exc.code == "timeout" else exc.code
                last_error = VisionProcessingError(error_code, exc.detail)
            except ImagePreprocessingError as exc:
                partial_failure = True
                last_error = VisionProcessingError(exc.code, exc.detail)
            except VisionProcessingError as exc:
                partial_failure = True
                last_error = exc

        if not prepared:
            error = last_error or VisionProcessingError(
                "resource_unavailable",
                "图片资源暂时不可用",
            )
            self._log_result(
                conversation_key=conversation_key,
                image_count=len(references),
                total_bytes=total_bytes,
                frame_count=0,
                content_hash="",
                cache_hit=False,
                success=False,
                started=started,
                error_category=error.code,
            )
            raise error

        aggregate_hash = _aggregate_hash(tuple(item.media_hash for item in prepared))
        cached = await self._analyses.find_cached(
            content_hash=aggregate_hash,
            analysis_mode=cache_mode,
            question_hash=question_hash,
            provider=self.provider_name,
            model=self.model_name,
            prompt_version=cache_prompt_version,
        )
        observation = _cached_observation(cached.observation_json) if cached else None
        if observation is not None:
            if partial_failure and not observation.partial_failure:
                observation = observation.model_copy(update={"partial_failure": True})
            self._log_result(
                conversation_key=conversation_key,
                image_count=len(prepared),
                total_bytes=total_bytes,
                frame_count=sum(len(item.frames) for item in prepared),
                content_hash=aggregate_hash,
                cache_hit=True,
                success=True,
                started=started,
            )
            return observation

        flight_key = "\x00".join(
            (
                aggregate_hash,
                cache_mode,
                question_hash,
                self.provider_name,
                self.model_name,
                cache_prompt_version,
                str(partial_failure),
            )
        )
        async with self._singleflight_guard:
            shared_future = self._singleflight.get(flight_key)
            is_leader = shared_future is None
            if shared_future is None:
                shared_future = asyncio.get_running_loop().create_future()
                self._singleflight[flight_key] = shared_future

        if is_leader:
            try:
                allowed = await self._rate_limiter.allow(
                    user_id=message.person_id or message.presence_id or "",
                    group_id=message.space_id,
                    per_user_per_minute=runtime.per_user_requests_per_minute,
                    per_group_per_minute=runtime.per_group_requests_per_minute,
                )
                if not allowed:
                    raise VisionProcessingError("rate_limited", "图片理解请求过于频繁，请稍后再试")
                try:
                    observation = await self._provider.analyze(
                        tuple(prepared),
                        effective_question,
                        options=VisionAnalysisOptions(
                            analysis_mode=mode,
                            thinking_enabled=runtime.thinking_enabled,
                            thinking_budget=runtime.thinking_budget,
                        ),
                    )
                except VisionError as exc:
                    raise VisionProcessingError(exc.code, exc.detail) from exc
                if partial_failure and not observation.partial_failure:
                    observation = observation.model_copy(update={"partial_failure": True})
                if not partial_failure:
                    source_reference = prepared_references[0]
                    expires_at = datetime.now(UTC) + timedelta(days=runtime.analysis_retention_days)
                    await self._analyses.save(
                        source_event_id=source_event_id,
                        segment_index=source_reference.segment_index or 0,
                        content_hash=aggregate_hash,
                        analysis_mode=cache_mode,
                        question_hash=question_hash,
                        provider=self.provider_name,
                        model=self.model_name,
                        prompt_version=cache_prompt_version,
                        observation_json=observation.model_dump_json(),
                        expires_at=expires_at,
                    )
            except asyncio.CancelledError:
                failure = VisionProcessingError("provider_cancelled", "共享图片理解请求已被取消")
                shared_future.set_exception(failure)
                shared_future.exception()
                raise
            except Exception as exc:
                shared_future.set_exception(exc)
                shared_future.exception()
                if isinstance(exc, VisionProcessingError):
                    error_code = exc.code
                else:
                    error_code = f"unexpected_{type(exc).__name__}"
                self._log_result(
                    conversation_key=conversation_key,
                    image_count=len(prepared),
                    total_bytes=total_bytes,
                    frame_count=sum(len(item.frames) for item in prepared),
                    content_hash=aggregate_hash,
                    cache_hit=False,
                    singleflight_shared=False,
                    success=False,
                    started=started,
                    error_category=error_code,
                )
                raise
            else:
                shared_future.set_result(observation)
            finally:
                async with self._singleflight_guard:
                    if self._singleflight.get(flight_key) is shared_future:
                        self._singleflight.pop(flight_key, None)
        else:
            observation = await asyncio.shield(shared_future)
            if partial_failure and not observation.partial_failure:
                observation = observation.model_copy(update={"partial_failure": True})
        self._log_result(
            conversation_key=conversation_key,
            image_count=len(prepared),
            total_bytes=total_bytes,
            frame_count=sum(len(item.frames) for item in prepared),
            content_hash=aggregate_hash,
            cache_hit=False,
            singleflight_shared=not is_leader,
            success=True,
            started=started,
        )
        return observation

    async def close(self) -> None:
        """Close provider and downloader clients after in-flight calls finish or cancel."""

        if self._closed:
            return
        self._closed = True
        await self._idle.wait()
        await self._resolver.close()
        await self._provider.close()

    def _log_result(
        self,
        *,
        conversation_key: str,
        image_count: int,
        total_bytes: int,
        frame_count: int,
        content_hash: str,
        cache_hit: bool,
        success: bool,
        started: float,
        error_category: str | None = None,
        singleflight_shared: bool = False,
    ) -> None:
        logger.info(
            "vision_analysis conversation_hash=%s image_count=%d total_bytes=%d "
            "frame_count=%d content_hash=%s provider=%s model=%s latency=%.3f "
            "cache_hit=%s singleflight_shared=%s success=%s error_category=%s",
            _conversation_hash(conversation_key),
            image_count,
            total_bytes,
            frame_count,
            content_hash[:12],
            self.provider_name,
            self.model_name,
            time.perf_counter() - started,
            cache_hit,
            singleflight_shared,
            success,
            error_category or "",
        )


def _conversation_hash(conversation_key: str) -> str:
    return hashlib.sha256(conversation_key.encode("utf-8")).hexdigest()[:12]


def _media_reference(
    attachment: MessageAttachment,
    *,
    message_id: str | None,
) -> MediaReference:
    return MediaReference(
        message_id=message_id,
        segment_index=attachment.segment_index,
        source="reply" if attachment.source == "reply" else "current",
        file=attachment.file,
        url=attachment.url,
        summary=attachment.summary,
        sub_type=attachment.sub_type,
        declared_size=attachment.file_size,
        emoji_id=attachment.emoji_id,
        emoji_package_id=attachment.emoji_package_id,
    )


def _analysis_mode(question: str, references: tuple[MediaReference, ...]) -> VisionAnalysisMode:
    lowered = question.casefold()
    if not lowered:
        if any(
            reference.emoji_id or reference.emoji_package_id or reference.summary
            for reference in references
        ):
            return "meme"
        return "general"
    if any(token in lowered for token in ("ocr", "文字", "写了什么", "什么字", "截图里的字")):
        return "ocr"
    if any(
        token in lowered
        for token in (
            "这是谁",
            "是谁",
            "什么角色",
            "哪个角色",
            "叫什么",
            "人物",
            "角色",
            "认得",
            "认识",
            "来自哪部",
            "哪个作品",
            "奶龙",
            "水上由岐",
        )
    ):
        return "character"
    if any(token in lowered for token in ("表情", "情绪", "这个梗", "什么意思")):
        return "meme"
    return "question"


def _aggregate_hash(hashes: tuple[str, ...]) -> str:
    if len(hashes) == 1:
        return hashes[0]
    return hashlib.sha256("\x00".join(hashes).encode("ascii")).hexdigest()


def _cache_prompt_version(
    base: str,
    runtime: VisionRuntimeConfig,
    references: tuple[MediaReference, ...],
) -> str:
    """Bind cache entries to HOT preprocessing limits and provider-visible hints."""

    hints = tuple(" ".join((item.summary or "").split())[:300] for item in references)
    material = "\x00".join(
        (
            str(runtime.max_images_per_turn),
            str(runtime.max_frames_per_turn),
            str(runtime.gif_max_frames),
            "native-reasoning-required-v1",
            str(runtime.thinking_budget),
            str(len(references)),
            *hints,
        )
    )
    variant = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{base[:30]}:{variant}:{base[-12:]}"


def _cached_observation(payload: str) -> VisualObservation | None:
    try:
        return VisualObservation.model_validate_json(payload)
    except (ValueError, ValidationError):
        return None


def _data_url_size(value: str) -> int:
    _, separator, encoded = value.partition(",")
    if not separator:
        return len(value)
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, (len(encoded) * 3) // 4 - padding)
