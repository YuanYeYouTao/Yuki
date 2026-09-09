"""Immutable provider-neutral models for bounded visual analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MediaSource = Literal["current", "reply"]
VisionAnalysisMode = Literal["general", "meme", "ocr", "question", "character"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MediaReference(_FrozenModel):
    """A trusted image reference extracted from one real OneBot event."""

    message_id: str | None = None
    segment_index: int | None = Field(default=None, ge=0)
    source: MediaSource = "current"
    file: str | None = Field(default=None, repr=False)
    url: str | None = Field(default=None, repr=False)
    summary: str | None = Field(default=None, repr=False)
    sub_type: str | None = None
    declared_size: int | None = Field(default=None, ge=0)
    emoji_id: str | None = None
    emoji_package_id: str | None = None


class DownloadedMedia(_FrozenModel):
    """A bounded in-memory media object; it is never persisted."""

    content: bytes = Field(repr=False)
    content_type: str | None
    content_hash: str
    byte_size: int = Field(ge=0)


class PreparedFrame(_FrozenModel):
    """One normalized frame ready for an OpenAI-compatible vision API."""

    content_hash: str
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_index: int = Field(ge=0)
    frame_count: int = Field(gt=0)
    data_url: str = Field(repr=False)


class PreparedVisualInput(_FrozenModel):
    """Prepared frames belonging to one source image."""

    media_hash: str
    frames: tuple[PreparedFrame, ...]
    animated: bool
    source: MediaSource
    summary_hint: str | None = None


class VisionAnalysisOptions(_FrozenModel):
    """Per-request controls for dynamic visual reasoning."""

    analysis_mode: VisionAnalysisMode = "general"
    thinking_enabled: bool = True
    thinking_budget: int = Field(default=6144, gt=0, le=32768)
    low_confidence_retry_threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    @field_validator("thinking_enabled")
    @classmethod
    def _enable_reasoning(cls, value: bool) -> bool:
        return True


class VisualCharacterCandidate(_FrozenModel):
    """One bounded fictional-character identity candidate."""

    name: str = Field(max_length=200)
    work: str = Field(default="", max_length=200)
    evidence: str = Field(default="", max_length=600)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class VisualItemObservation(_FrozenModel):
    """Structured observation for one input image, not a final chat answer."""

    index: int = Field(ge=1)
    description: str = Field(max_length=1200)
    ocr_text: str = Field(default="", max_length=2000)
    expression: str = Field(default="", max_length=1200)
    meme_intent: str = Field(default="", max_length=1200)
    is_emoji: bool | None = None
    emotion_tags: tuple[str, ...] = Field(default=(), max_length=20)
    usage_scenarios: tuple[str, ...] = Field(default=(), max_length=20)
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    recognized_character: str = Field(default="", max_length=200)
    franchise: str = Field(default="", max_length=200)
    character_candidates: tuple[VisualCharacterCandidate, ...] = Field(default=(), max_length=3)
    notable_objects: tuple[str, ...] = Field(default=(), max_length=20)
    uncertainty: str = Field(default="", max_length=1200)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class VisualObservation(_FrozenModel):
    """Complete provider result supplied to the text model as untrusted data."""

    items: tuple[VisualItemObservation, ...]
    overall_description: str = Field(max_length=2000)
    partial_failure: bool = False
    provider: str = "unknown"
    model: str = "unknown"
    latency_seconds: float = Field(default=0.0, ge=0.0)
