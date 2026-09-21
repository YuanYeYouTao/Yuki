"""Focused contracts for the 1.7 persistent emoji subsystem."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from qq_ai_bot.admin.models import EmojiRuntimeConfig, VisionRuntimeConfig
from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment
from qq_ai_bot.emoji.classifier import EmojiClassifier
from qq_ai_bot.emoji.detector import EmojiCandidateDetector
from qq_ai_bot.emoji.grid import EmojiGridBuilder
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.models import (
    EmojiAnalysis,
    EmojiCollectionMode,
    EmojiLifecycleStatus,
    EmojiReplyMode,
    EmojiReplyPlan,
    EmojiSelectionRequest,
)
from qq_ai_bot.emoji.replacement import EmojiReplacementService
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.retriever import EmojiRetriever, RankedEmoji
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import MediaAnalysisRepository
from qq_ai_bot.plugin_host.emoji_adapter import PluginEmojiSelectionSignalAdapter
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.vision.fake import FakeVisionProvider
from qq_ai_bot.vision.models import VisualItemObservation, VisualObservation
from yuki_plugin_sdk.models import EmojiSelectionSignal
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import EmojiSelectionSignalRegistration


def _runtime(**updates: object) -> EmojiRuntimeConfig:
    base = EmojiRuntimeConfig(
        enabled=True,
        collection_enabled=True,
        collection_mode="likely",
        collect_private=True,
        collect_group=True,
        auto_adopt_enabled=True,
        auto_adopt_min_confidence=0.78,
        pool_capacity=None,
        replacement_mode="score",
        selector_enabled=True,
        selector_candidate_count=3,
        selector_score_gap=0.75,
        selector_timeout_seconds=2,
        near_duplicate_enabled=True,
        near_duplicate_distance=6,
        same_emoji_cooldown_seconds=0,
        scope_repeat_cooldown_seconds=0,
        cache_retention_days=30,
        worker_batch_size=10,
        worker_poll_seconds=2,
        worker_lease_seconds=120,
        worker_max_attempts=3,
        worker_retry_delay_seconds=30,
        analysis_version="emoji-v1",
    )
    return replace(base, **updates)


def _image_bytes(image_format: str = "PNG", *, animated: bool = False) -> bytes:
    output = io.BytesIO()
    first = Image.new("RGBA", (24, 20), "red")
    if animated:
        second = Image.new("RGBA", (24, 20), "blue")
        first.save(
            output,
            format=image_format,
            save_all=True,
            append_images=[second],
            duration=80,
            loop=0,
        )
    else:
        first.save(output, format=image_format)
    return output.getvalue()


def _vision_runtime() -> VisionRuntimeConfig:
    return VisionRuntimeConfig(
        max_images_per_turn=5,
        max_frames_per_turn=16,
        gif_max_frames=8,
        thinking_enabled=False,
        thinking_budget=1,
        low_confidence_retry_threshold=0.65,
        per_user_requests_per_minute=20,
        per_group_requests_per_minute=60,
        analysis_retention_days=7,
    )


class _StaticEmojiRetriever(EmojiRetriever):
    def __init__(self, candidates: tuple[RankedEmoji, ...]) -> None:
        self._candidates = candidates

    async def retrieve(
        self,
        request: EmojiSelectionRequest,
        *,
        runtime: EmojiRuntimeConfig,
    ) -> tuple[RankedEmoji, ...]:
        del request, runtime
        return self._candidates


async def _selector_with_scores(
    database: Database,
    tmp_path: Path,
    scores: tuple[float, ...],
    provider: FakeVisionProvider,
) -> tuple[EmojiSelector, tuple[RankedEmoji, ...]]:
    storage = EmojiStorage(tmp_path / "emoji")
    repository = EmojiRepository(database)
    candidates: list[RankedEmoji] = []
    for color, score in zip(("red", "green", "blue"), scores, strict=True):
        output = io.BytesIO()
        Image.new("RGB", (24, 20), color).save(output, format="PNG")
        media = storage.inspect(output.getvalue(), near_duplicate_enabled=False)
        storage.persist(output.getvalue(), media)
        asset, _ = await repository.record_candidate(
            media,
            source_event_id=None,
            user_id=None,
            group_id=None,
            source_sub_type="emoji",
            source_emoji_id="",
            source_package_id="",
        )
        candidates.append(RankedEmoji(asset=asset, score=score))
    ranked = tuple(candidates)
    return (
        EmojiSelector(
            retriever=_StaticEmojiRetriever(ranked),
            grid_builder=EmojiGridBuilder(storage),
            preprocessor=ImagePreprocessor(),
            provider=provider,
        ),
        ranked,
    )


def test_candidate_detector_honors_metadata_likely_and_all_images() -> None:
    detector = EmojiCandidateDetector()
    ordinary = MessageAttachment(kind=AttachmentKind.IMAGE, label="截图")
    likely = MessageAttachment(kind=AttachmentKind.IMAGE, label="图片", summary="一张聊天表情")
    explicit = MessageAttachment(kind=AttachmentKind.IMAGE, label="图片", emoji_id="123")

    assert not detector.is_candidate(ordinary, EmojiCollectionMode.METADATA_ONLY)
    assert detector.is_candidate(explicit, EmojiCollectionMode.METADATA_ONLY)
    assert detector.is_candidate(likely, EmojiCollectionMode.LIKELY)
    assert detector.is_candidate(ordinary, EmojiCollectionMode.ALL_IMAGES)


def test_storage_uses_real_format_and_can_disable_perceptual_hash(tmp_path: Path) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    content = _image_bytes("PNG")
    media = storage.inspect(content, near_duplicate_enabled=False)

    assert media.image_format == "PNG"
    assert media.relative_path.endswith(".png")
    assert media.perceptual_hash is None
    storage.persist(content, media)
    assert storage.read(media.relative_path) == content
    assert storage.resolve(media.preview_relative_path).read_bytes().startswith(b"RIFF")
    assert not tuple(storage.root.rglob(".emoji-*.tmp"))


def test_storage_preserves_animated_gif_and_builds_static_preview(tmp_path: Path) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    content = _image_bytes("GIF", animated=True)
    media = storage.inspect(content, near_duplicate_enabled=True)
    storage.persist(content, media)

    assert media.animated is True
    assert media.frame_count == 2
    assert storage.read(media.relative_path) == content
    with Image.open(storage.resolve(media.preview_relative_path)) as preview:
        assert preview.n_frames == 1


@pytest.mark.asyncio
async def test_repository_exact_dedup_updates_seen_count(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    content = _image_bytes()
    media = storage.inspect(content, near_duplicate_enabled=True)
    repository = EmojiRepository(database)

    first, created = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )
    second, created_again = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )

    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert second.seen_count == 2


@pytest.mark.asyncio
async def test_lifecycle_classifies_and_auto_adopts_without_review(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    media = storage.inspect(_image_bytes(), near_duplicate_enabled=True)
    repository = EmojiRepository(database)
    asset, _ = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )
    lifecycle = EmojiLifecycleService(repository)
    result = await lifecycle.apply_analysis(
        asset,
        EmojiAnalysis(
            is_emoji=True,
            description="开心地挥手",
            emotion_tags=("开心",),
            usage_scenarios=("打招呼",),
            confidence=0.95,
            animated=False,
            analysis_version="emoji-v1",
        ),
        runtime=_runtime(),
    )

    assert result.status is EmojiLifecycleStatus.ADOPTED
    assert await repository.adopted_count() == 1
    assert not hasattr(Settings(), "emoji_review_enabled")

    reanalyzed = await lifecycle.apply_analysis(
        result,
        EmojiAnalysis(
            is_emoji=True,
            description="重新识别后的开心挥手",
            emotion_tags=("开心",),
            usage_scenarios=("打招呼",),
            confidence=0.96,
            animated=False,
            analysis_version="emoji-v2",
        ),
        runtime=_runtime(pool_capacity=1, replacement_mode="off"),
    )
    assert reanalyzed.status is EmojiLifecycleStatus.ADOPTED
    assert await repository.adopted_count() == 1

    no_longer_emoji = await lifecycle.apply_analysis(
        reanalyzed,
        EmojiAnalysis(
            is_emoji=False,
            description="重新识别后确认是普通照片",
            confidence=0.98,
            animated=False,
            analysis_version="emoji-v3",
        ),
        runtime=_runtime(pool_capacity=1, replacement_mode="off"),
    )
    assert no_longer_emoji.status is EmojiLifecycleStatus.REJECTED
    assert await repository.adopted_count() == 0


@pytest.mark.asyncio
async def test_explicit_emoji_request_bypasses_scope_repeat_cooldown(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    content = _image_bytes()
    media = storage.inspect(content, near_duplicate_enabled=False)
    storage.persist(content, media)
    repository = EmojiRepository(database)
    asset, _ = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )
    await repository.adopt_scope(asset.id, scope_type="global")
    await repository.mark_used(
        asset.id,
        actor_user_id="10001",
        group_id=None,
        trigger_message_id="first",
        source="test",
    )
    retriever = EmojiRetriever(repository, storage)
    runtime = _runtime(scope_repeat_cooldown_seconds=60)

    optional = await retriever.retrieve(
        EmojiSelectionRequest(actor_user_id="10001", mode=EmojiReplyMode.OPTIONAL),
        runtime=runtime,
    )
    explicit = await retriever.retrieve(
        EmojiSelectionRequest(actor_user_id="10001", mode=EmojiReplyMode.PREFERRED),
        runtime=runtime,
    )

    assert optional == ()
    assert [item.asset.id for item in explicit] == [asset.id]


@pytest.mark.asyncio
async def test_optional_emoji_uses_local_top_candidate_without_vision(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeVisionProvider(delay_seconds=10)
    selector, candidates = await _selector_with_scores(
        database, tmp_path, (5.0, 4.9, 4.8), provider
    )

    result = await selector.select(
        EmojiSelectionRequest(
            actor_user_id="10001",
            goal="自然回应",
            mode=EmojiReplyMode.OPTIONAL,
        ),
        runtime=_runtime(),
        vision_runtime=_vision_runtime(),
    )

    assert result.emoji_id == candidates[0].asset.id
    assert result.selected_by == "coarse"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_explicit_emoji_only_uses_vision_when_scores_are_close(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeVisionProvider(
        lambda _inputs, _question: VisualObservation(
            items=(VisualItemObservation(index=1, description="选择第二张", ocr_text="2"),),
            overall_description="2",
            provider="fake",
            model="fake",
            latency_seconds=0,
        )
    )
    selector, candidates = await _selector_with_scores(
        database, tmp_path, (5.0, 4.8, 4.7), provider
    )

    result = await selector.select(
        EmojiSelectionRequest(
            actor_user_id="10001",
            goal="发个开心的表情",
            explicit_request=True,
            mode=EmojiReplyMode.EMOJI_ONLY,
        ),
        runtime=_runtime(selector_score_gap=0.5),
        vision_runtime=_vision_runtime(),
    )

    assert result.emoji_id == candidates[1].asset.id
    assert result.selected_by == "vision"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_explicit_emoji_skips_vision_for_clear_local_winner(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeVisionProvider(delay_seconds=10)
    selector, candidates = await _selector_with_scores(
        database, tmp_path, (5.0, 3.0, 2.0), provider
    )

    result = await selector.select(
        EmojiSelectionRequest(
            actor_user_id="10001",
            goal="发个开心的表情",
            explicit_request=True,
            mode=EmojiReplyMode.PREFERRED,
        ),
        runtime=_runtime(selector_score_gap=0.5),
        vision_runtime=_vision_runtime(),
    )

    assert result.emoji_id == candidates[0].asset.id
    assert result.selected_by == "coarse"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_explicit_emoji_vision_timeout_falls_back_to_local_top(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeVisionProvider(delay_seconds=0.1)
    selector, candidates = await _selector_with_scores(
        database, tmp_path, (5.0, 4.9, 4.8), provider
    )

    result = await selector.select(
        EmojiSelectionRequest(
            actor_user_id="10001",
            goal="发个开心的表情",
            explicit_request=True,
            mode=EmojiReplyMode.PREFERRED,
        ),
        runtime=_runtime(selector_score_gap=0.5, selector_timeout_seconds=0.001),
        vision_runtime=_vision_runtime(),
    )

    assert result.emoji_id == candidates[0].asset.id
    assert result.selected_by == "coarse"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_photo_rejected_and_illegal_transition_is_explicit(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    media = storage.inspect(_image_bytes(), near_duplicate_enabled=False)
    repository = EmojiRepository(database)
    asset, _ = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="",
        source_emoji_id="",
        source_package_id="",
    )
    lifecycle = EmojiLifecycleService(repository)
    rejected = await lifecycle.apply_analysis(
        asset,
        EmojiAnalysis(
            is_emoji=False,
            description="普通风景照片",
            confidence=0.99,
            animated=False,
            analysis_version="emoji-v1",
        ),
        runtime=_runtime(),
    )
    assert rejected.status is EmojiLifecycleStatus.REJECTED
    with pytest.raises(ValueError, match="illegal emoji transition"):
        await lifecycle.transition(rejected.id, EmojiLifecycleStatus.ADOPTED)


@pytest.mark.asyncio
async def test_classifier_reuses_compatible_media_analysis_without_provider_call(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    content = _image_bytes()
    media = storage.inspect(content, near_duplicate_enabled=False)
    storage.persist(content, media)
    repository = EmojiRepository(database)
    asset, _ = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )
    analyses = MediaAnalysisRepository(database)
    observation = VisualObservation(
        items=(
            VisualItemObservation(
                index=1,
                description="缓存里的挥手表情",
                is_emoji=True,
                emotion_tags=("开心",),
                usage_scenarios=("问候",),
                confidence=0.9,
            ),
        ),
        overall_description="缓存里的挥手表情",
        provider="fake",
        model="fake",
        latency_seconds=0,
    )
    await analyses.save(
        source_event_id=None,
        segment_index=0,
        content_hash=asset.sha256,
        analysis_mode="meme",
        question_hash="",
        provider="fake",
        model="fake",
        prompt_version="vision-cache:emoji-v1",
        observation_json=observation.model_dump_json(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    provider = FakeVisionProvider()
    classifier = EmojiClassifier(
        provider=provider,
        preprocessor=ImagePreprocessor(),
        storage=storage,
        analyses=analyses,
    )

    result = await classifier.classify(
        asset,
        analysis_version="emoji-v1",
        max_frames=1,
        thinking_enabled=False,
        thinking_budget=0,
    )

    assert result.description == "缓存里的挥手表情"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_plugin_signal_can_only_adjust_existing_candidates(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    repository = EmojiRepository(database)
    assets = []
    for color in ("red", "blue"):
        output = io.BytesIO()
        Image.new("RGB", (20, 20), color).save(output, format="PNG")
        media = storage.inspect(output.getvalue(), near_duplicate_enabled=False)
        asset, _ = await repository.record_candidate(
            media,
            source_event_id=None,
            user_id=None,
            group_id=None,
            source_sub_type="emoji",
            source_emoji_id="",
            source_package_id="",
        )
        assets.append(asset)

    async def invalid(_context: object) -> EmojiSelectionSignal:
        return EmojiSelectionSignal(
            candidate_id="not-a-core-candidate",
            score_delta=10,
            reason="invalid id",
            confidence=1,
        )

    async def valid(_context: object) -> EmojiSelectionSignal:
        return EmojiSelectionSignal(
            candidate_id=assets[1].id,
            score_delta=10,
            reason="prefer blue",
            confidence=1,
        )

    registry = ExtensionRegistry()
    registrar = registry.registrar("com.example.emoji", (PluginPermission.EMOJI_HOOK,))
    registrar.register_emoji_selection_signal(
        EmojiSelectionSignalRegistration(name="invalid", provider=invalid)  # type: ignore[arg-type]
    )
    registrar.register_emoji_selection_signal(
        EmojiSelectionSignalRegistration(name="valid", provider=valid)  # type: ignore[arg-type]
    )
    adapter = PluginEmojiSelectionSignalAdapter(registry, timeout_seconds=1)
    adjusted = await adapter.adjust(
        (RankedEmoji(assets[0], 2), RankedEmoji(assets[1], 1)),
        EmojiSelectionRequest(actor_user_id="10001", goal="开心"),
    )

    assert adjusted[0].asset.id == assets[1].id
    assert {item.asset.id for item in adjusted} == {asset.id for asset in assets}


@pytest.mark.asyncio
async def test_llm_replacement_selects_only_an_existing_candidate(
    database: Database, tmp_path: Path
) -> None:
    storage = EmojiStorage(tmp_path / "emoji")
    repository = EmojiRepository(database)
    assets = []
    for color in ("red", "blue"):
        output = io.BytesIO()
        Image.new("RGB", (20, 20), color).save(output, format="PNG")
        media = storage.inspect(output.getvalue(), near_duplicate_enabled=False)
        asset, _ = await repository.record_candidate(
            media,
            source_event_id=None,
            user_id=None,
            group_id=None,
            source_sub_type="emoji",
            source_emoji_id="",
            source_package_id="",
        )
        assets.append(asset)

    provider = FakeLLMProvider(lambda _request: json.dumps({"emoji_id": assets[1].id}))
    replacement = EmojiReplacementService(
        provider,
        model="fake",
        max_prompt_characters=4000,
    )
    selected = await replacement.choose(tuple(assets), mode="llm")
    assert selected is not None
    assert selected.id == assets[1].id

    invalid_provider = FakeLLMProvider(lambda _request: "not a candidate")
    fallback = EmojiReplacementService(
        invalid_provider,
        model="fake",
        max_prompt_characters=4000,
    )
    fallback_selected = await fallback.choose(tuple(assets), mode="hybrid")
    assert fallback_selected is not None
    assert fallback_selected.id in {asset.id for asset in assets}


def test_planner_emoji_plan_rejects_asset_identifiers() -> None:
    with pytest.raises(ValidationError):
        EmojiReplyPlan.model_validate(
            {"mode": "preferred", "goal": "开心", "emoji_id": "forbidden"}
        )


def test_automation_registry_exposes_scoped_emoji_actions() -> None:
    registry = build_capability_registry()
    assert registry.require("emoji.send").required_permission is PermissionLevel.USER
    assert registry.require("emoji.send_by_id").required_permission is PermissionLevel.USER


def test_fixed_automation_emoji_id_cannot_come_from_step_output() -> None:
    settings = Settings.model_validate({"SUPERUSERS": "9000"})
    validator = AutomationValidator(settings=settings, registry=build_capability_registry())
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "表情提醒",
            "schedule": {"type": "after", "seconds": 60},
            "steps": [
                {
                    "id": "send",
                    "call": "emoji.send_by_id",
                    "arguments": {
                        "user_id": "10001",
                        "emoji_id": "${other.emoji_id}",
                    },
                }
            ],
        }
    )
    with pytest.raises(ValueError):
        validator.validate(
            script,
            CreationProvenance(
                creator_user_id="10001",
                bot_user_id="9999",
                message_id="m1",
                original_text="一分钟后给我发表情",
                current_group_id=None,
                mentioned_user_ids=(),
                permission=PermissionLevel.USER,
            ),
            now_utc=datetime.now(UTC),
        )
