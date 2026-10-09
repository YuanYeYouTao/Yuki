"""Domain models for explicit administrator capabilities and runtime settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from qq_ai_bot.config import Settings

ConfigValue = str | int | float | bool | None
ConfigValueType = Literal["string", "integer", "number", "boolean", "enum"]


class ConfigApplyMode(StrEnum):
    """How a registered configuration value becomes effective."""

    HOT = "hot"
    FUTURE_ONLY = "future_only"
    RESTART_REQUIRED = "restart_required"
    IMMUTABLE = "immutable"
    SECRET = "secret"


class ConfigScopeType(StrEnum):
    """Supported override scopes, ordered elsewhere by specificity."""

    GLOBAL = "global"
    GROUP = "group"
    USER = "user"


@dataclass(frozen=True, slots=True)
class ConfigSpec:
    """One explicitly exposed configuration capability."""

    key: str
    display_name: str
    description: str
    aliases: tuple[str, ...]
    value_type: ConfigValueType
    minimum: float | None
    maximum: float | None
    choices: tuple[str, ...]
    allowed_scopes: tuple[ConfigScopeType, ...]
    apply_mode: ConfigApplyMode
    permission: str
    sensitive: bool
    env_alias: str | None
    default_getter: Callable[[Settings], ConfigValue]
    settings_fields: tuple[str, ...] = ()
    category: str = ""

    @property
    def mutable(self) -> bool:
        """Return whether a validated database override may be written."""

        return self.apply_mode not in {
            ConfigApplyMode.IMMUTABLE,
            ConfigApplyMode.SECRET,
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfigValue:
    """A resolved value plus provenance, without leaking secret material."""

    key: str
    value: ConfigValue
    source: str
    scope_type: ConfigScopeType | None
    scope_id: str
    apply_mode: ConfigApplyMode
    pending_restart: bool = False
    configured: bool | None = None


@dataclass(frozen=True, slots=True)
class ConfigChangeResult:
    """Truthful result returned by deterministic commands and model tools."""

    success: bool
    key: str
    scope_type: ConfigScopeType
    scope_id: str
    before: ConfigValue = None
    after: ConfigValue = None
    apply_mode: ConfigApplyMode | None = None
    pending_restart: bool = False
    change_id: int | None = None
    version: int | None = None
    error_category: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AdminOperationEvent:
    """Safe projection of one persisted administrator operation."""

    id: int
    actor_user_id: str
    trigger_message_id: str
    conversation_key: str
    capability: str
    operation: str
    target_type: str
    target_id: str
    before: object
    after: object
    success: bool
    error_category: str | None
    duration_seconds: float
    created_at: datetime
    actor_principal_kind: str | None = None
    actor_principal_id: str | None = None
    control_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdminActor:
    """Authority derived only from the current transport event."""

    user_id: str
    is_superuser: bool
    trigger_message_id: str
    conversation_key: str
    current_group_id: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    current_message_text: str = ""
    bot_user_id: str = ""
    decision_actor_type: str = "command"
    decision_actor_id: str | None = None
    trigger_event_id: int | None = None
    canonical_conversation_id: str | None = None
    ingress_presence_id: str | None = None


@dataclass(frozen=True, slots=True)
class ControlAuditRef:
    """Transport-neutral audit correlation already resolved at the adapter."""

    user_id: str
    trigger_message_id: str = ""
    conversation_key: str = ""
    bot_user_id: str = ""
    decision_actor_type: str = "command"
    decision_actor_id: str | None = None
    trigger_event_id: int | None = None
    canonical_conversation_id: str | None = None
    ingress_presence_id: str | None = None
    principal_kind: str | None = None
    principal_id: str | None = None
    control_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextRuntimeConfig:
    local_event_limit: int
    window_tokens: int = 96000
    work_window_tokens: int = 128000
    compaction_trigger_ratio: float = 0.90
    compaction_target_ratio: float = 0.60
    work_compaction_trigger_ratio: float = 0.90
    work_compaction_target_ratio: float = 0.50
    compaction_output_tokens: int = 32768
    rollup_output_tokens: int = 32768
    rollup_summary_characters: int = 16384
    compaction_window_tokens: int = 90000


@dataclass(frozen=True, slots=True)
class MemoryRetrievalRuntimeConfig:
    retrieval_enabled: bool
    max_referenced_targets: int
    lexical_candidate_limit: int
    context_limit_per_entity: int
    overview_limit_per_entity: int
    query_term_limit: int
    short_query_fallback_enabled: bool
    self_enabled: bool = False
    semantic_enabled: bool = True
    semantic_candidate_limit: int = 50
    hybrid_lexical_weight: float = 1.0
    hybrid_semantic_weight: float = 1.0
    hybrid_rrf_k: int = 60
    maintenance_enabled: bool = True
    maintenance_interval_seconds: float = 300.0
    maintenance_batch_limit: int = 100


@dataclass(frozen=True, slots=True)
class ConversationRuntimeConfig:
    """Effective autonomous-conversation policy for one turn."""

    autonomous_enabled: bool
    autonomous_debounce_seconds: float
    autonomous_admission_threshold: int
    autonomous_batch_limit: int
    autonomous_presence_window_seconds: int
    interrupt_autonomous_on_new_message: bool
    semantic_participation_enabled: bool = False


@dataclass(frozen=True, slots=True)
class ReplyRuntimeConfig:
    delay_min_seconds: float
    delay_max_seconds: float
    max_qq_message_chars: int
    hard_max_messages: int


@dataclass(frozen=True, slots=True)
class PluginRuntimeConfig:
    """Hot plugin limits that never include credentials or installation paths."""

    hook_timeout_seconds: float
    max_prompt_fragment_characters: int
    max_prompt_characters_per_plugin: int
    max_total_prompt_characters: int


@dataclass(frozen=True, slots=True)
class LLMRuntimeConfig:
    model: str
    timeout_seconds: float
    max_retries: int
    temperature: float
    max_output_tokens: int
    thinking_enabled: bool | None


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    max_tool_calls: int
    max_model_requests: int
    tool_result_max_characters: int


@dataclass(frozen=True, slots=True)
class WorkStorageRuntimeConfig:
    """Deployment-wide physical storage admission, independent of model windows."""

    total_max_bytes: int = 2 * 1024**3
    object_max_bytes: int = 64 * 1024**2
    disk_reserve_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        if min(self.total_max_bytes, self.object_max_bytes, self.disk_reserve_bytes) <= 0:
            raise ValueError("work protocol storage limits must be positive")
        if self.object_max_bytes > self.total_max_bytes:
            raise ValueError("work protocol object limit must not exceed total capacity")


@dataclass(frozen=True, slots=True)
class ToolingRuntimeConfig:
    max_parallel_calls: int
    result_token_budget: int | None
    result_item_limit: int | None
    result_artifact_enabled: bool
    result_artifact_retention_seconds: int


@dataclass(frozen=True, slots=True)
class WebRuntimeConfig:
    search_max_results: int
    extract_max_results: int
    max_calls_per_turn: int
    tool_result_max_characters: int
    source_retention_days: int
    source_max_runs_per_conversation: int
    mode: str = "disabled"


@dataclass(frozen=True, slots=True)
class VisionRuntimeConfig:
    max_images_per_turn: int
    max_frames_per_turn: int
    gif_max_frames: int
    thinking_enabled: bool
    thinking_budget: int
    low_confidence_retry_threshold: float
    per_user_requests_per_minute: int
    per_group_requests_per_minute: int
    analysis_retention_days: int
    video_max_duration_seconds: int = 600
    video_sample_interval_seconds: int = 5
    video_max_frames: int = 16
    video_max_download_bytes: int = 209_715_200


@dataclass(frozen=True, slots=True)
class EmojiRuntimeConfig:
    """Effective collection, pool, selection, and worker policy for one turn."""

    enabled: bool
    collection_enabled: bool
    collection_mode: str
    collect_private: bool
    collect_group: bool
    auto_adopt_enabled: bool
    auto_adopt_min_confidence: float
    pool_capacity: int | None
    replacement_mode: str
    selector_enabled: bool
    selector_candidate_count: int
    selector_score_gap: float
    selector_timeout_seconds: float
    near_duplicate_enabled: bool
    near_duplicate_distance: int
    same_emoji_cooldown_seconds: int
    scope_repeat_cooldown_seconds: int
    cache_retention_days: int
    worker_batch_size: int
    worker_poll_seconds: float
    worker_lease_seconds: int
    worker_max_attempts: int
    worker_retry_delay_seconds: float
    analysis_version: str


@dataclass(frozen=True, slots=True)
class RuntimeConfigSnapshot:
    """One internally consistent runtime view for an incoming message."""

    plugins: PluginRuntimeConfig
    context: ContextRuntimeConfig
    memory: MemoryRetrievalRuntimeConfig
    reply: ReplyRuntimeConfig
    llm: LLMRuntimeConfig
    agent: AgentRuntimeConfig
    web: WebRuntimeConfig
    vision: VisionRuntimeConfig
    emoji: EmojiRuntimeConfig
    conversation: ConversationRuntimeConfig
    tooling: ToolingRuntimeConfig | None = None
    work_storage: WorkStorageRuntimeConfig = WorkStorageRuntimeConfig()

    def conversation_policy(self) -> ConversationRuntimeConfig:
        """Return the autonomous-conversation policy for this snapshot."""

        return self.conversation
