"""Environment-driven application settings with safe defaults."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from yuki.domain.audio import ASRProtocol
from yuki.domain.messages import ReasoningEffort
from yuki.settings_domains import (
    AppSettings,
    ASRSettings,
    AutomationSettings,
    ConversationSettings,
    MemorySettings,
    ModelRuntimeSettings,
    OneBotSettings,
    PluginSettings,
    ToolingSettings,
    VisionSettings,
    WebSettings,
    validate_direct_command_bindings,
)
from yuki.web.models import WebMode


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """Human-facing identity shared by deterministic bot subsystems."""

    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SoulDocument:
    """One actual source document; the revision identifies its original UTF-8 bytes."""

    content: str
    revision: str


class Settings(BaseSettings):
    """Configuration loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    admin_api_url: str = "http://127.0.0.1:8080"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "INFO"
    log_message_content: bool = False
    workspace_directory: Path = Path("workspace")
    conversation_media_cache_directory: Path = Path("data/conversation-media-cache")
    social_transfer_directory: Path = Path("social-transfer")
    social_gateway_transfer_directory: str = ""
    sandbox_socket: Path = Path("/run/yuki-sandbox/manager.sock")
    # Declaration mode is fixed at startup; dispatch still checks current authority.
    code_mode_enabled: bool = False
    # Code Mode native worker: explicit path and pinned digest.
    code_mode_worker_path: Path | None = None
    code_mode_worker_sha256: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    code_mode_launcher_path: Path | None = None
    code_mode_launcher_sha256: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    code_mode_max_feed_seconds: float = Field(default=10.0, gt=0, le=120)
    code_mode_max_memory_bytes: int = Field(default=64 * 1024 * 1024, ge=1 << 20, le=1 << 30)
    code_mode_max_output_bytes: int = Field(default=64 * 1024, ge=1024, le=1 << 22)
    code_mode_max_snapshot_bytes: int = Field(default=8 << 20, ge=1 << 16, le=1 << 27)
    code_mode_request_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    code_mode_max_worker_processes: int = Field(default=1, ge=1, le=32)
    code_mode_foreground_reserved_processes: int = Field(default=0, ge=0, le=31)

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_poll_interval_seconds: float = Field(default=1.0, gt=0)

    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_domain: Literal["https://open.feishu.cn", "https://open.larksuite.com"] = (
        "https://open.feishu.cn"
    )

    onebot_access_token: str = ""
    enabled_groups_csv: str = Field(default="", validation_alias="ENABLED_GROUPS")
    ignored_bot_users_csv: str = Field(default="", validation_alias="IGNORED_BOT_USERS")
    ai_prefix: str = "!ai"

    bot_aliases_csv: str = Field(
        default="",
        validation_alias="BOT_ALIASES",
    )
    soul_file: Path = Path("character/SOUL.md")
    _character_name: str = PrivateAttr(default="")
    _character_revision: int = PrivateAttr(default=-1)
    _soul_document: SoulDocument = PrivateAttr(default=SoulDocument("", ""))

    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    execution_trace_retention_days: int = Field(default=30, ge=1, le=365)
    execution_trace_max_payload_bytes: int = Field(
        default=16 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024
    )
    llm_temperature: float = 0.7
    llm_max_output_tokens: int = 8192
    llm_thinking_enabled: bool | None = True
    llm_reasoning_effort: ReasoningEffort | None = None

    llm_flash_base_url: str = ""
    llm_flash_api_key: str = Field(default="", repr=False)
    llm_flash_model: str = ""
    model_profiles_file: Path = Path("webui-config/model_profiles.toml")
    model_stats_recent_error_limit: int = 5

    database_url: str = "sqlite+aiosqlite:///./data/yuki.db"
    control_operators_file: Path | None = None
    processed_event_cleanup_seconds: int = 3600
    # Chat and Work share the hot-configured automatic compact trigger.
    context_compact_trigger_tokens: int = Field(default=96000, gt=0)

    global_llm_concurrency: int = 4
    per_user_requests_per_minute: int = 10
    per_group_requests_per_minute: int = 30
    max_input_characters: int = 4000
    max_output_characters: int = 12000
    max_qq_message_chars: int = 1800
    daily_chat_message_delay_min_seconds: float = 1.0
    daily_chat_message_delay_max_seconds: float = 2.0

    observe_enabled_groups: bool = True
    recent_history_tool_limit: int = 20
    local_context_event_limit: int = 2_048
    memory_batch_seconds: float = 30.0
    memory_batch_trigger_count: int = Field(default=12, gt=0)
    memory_batch_max_events: int = Field(default=12, gt=0)
    memory_batch_max_characters: int = Field(default=8000, gt=0)
    memory_batch_max_wait_seconds: float = Field(default=3600.0, ge=0)
    memory_batch_max_output_tokens: int = Field(default=4096, gt=0)
    memory_retrieval_enabled: bool = True
    self_memory_enabled: bool = True
    memory_self_reflection_enabled: bool = True
    memory_self_reflection_schedule_hours: str = "4,12,20"
    memory_self_reflection_timezone: str = "Asia/Shanghai"
    memory_self_reflection_poll_seconds: float = 60.0
    memory_self_reflection_max_batches_per_run: int = Field(
        default=32,
        validation_alias=AliasChoices(
            "MEMORY_SELF_REFLECTION_MAX_BATCHES_PER_RUN",
            "MEMORY_SELF_REFLECTION_MAX_SESSIONS_PER_RUN",
        ),
    )
    memory_self_reflection_max_daily_calls: int = 96
    memory_self_reflection_event_threshold: int = 50
    memory_self_reflection_character_threshold: int = 8000
    memory_self_reflection_max_wait_seconds: float = 28800.0
    memory_self_reflection_max_events: int = 200
    memory_self_reflection_max_characters: int = 16000
    memory_self_reflection_max_output_tokens: int = 32768
    memory_self_reflection_timeout_seconds: float = 180.0
    memory_self_reflection_tool_receipt_characters: int = 2000
    memory_self_reflection_tool_receipt_retention_days: int = 7
    memory_lexical_candidate_limit: int = 50
    memory_context_limit_per_entity: int = 4
    memory_overview_limit_per_entity: int = 10
    memory_query_term_limit: int = 12
    memory_short_query_fallback_enabled: bool = True
    memory_semantic_enabled: bool = True
    memory_semantic_candidate_limit: int = 50
    memory_hybrid_lexical_weight: float = 1.0
    memory_hybrid_semantic_weight: float = 1.0
    memory_hybrid_rrf_k: int = 60
    memory_dream_enabled: bool = True
    memory_dream_schedule_hour: int = Field(default=5, ge=0, le=23)
    memory_dream_timezone: str = "Asia/Shanghai"
    memory_dream_poll_seconds: float = Field(default=60.0, gt=0)
    memory_dream_max_clusters_per_run: int = Field(default=12, gt=0)
    memory_dream_max_model_calls_per_run: int = Field(default=24, gt=0)
    memory_dream_similarity_threshold: float = Field(default=0.70, ge=-1, le=1)
    memory_dream_max_cluster_size: int = Field(default=6, gt=0)
    memory_dream_max_input_characters: int = Field(default=24_000, gt=0)
    memory_dream_max_output_tokens: int = Field(default=4096, gt=0)
    memory_dream_evidence_per_fact: int = Field(default=2, ge=1)
    memory_dream_evidence_excerpt_characters: int = Field(default=300, gt=0)
    memory_maintenance_enabled: bool = True
    memory_maintenance_interval_seconds: float = 300.0
    memory_maintenance_batch_limit: int = 100

    memory_embedding_enabled: bool = True
    memory_embedding_provider: str = "qwen_dashscope"
    memory_embedding_base_url: str = ""
    memory_embedding_api_key: str = Field(default="", repr=False)
    memory_embedding_model: str = "qwen3.7-text-embedding"
    memory_embedding_dimensions: int = 1024
    memory_embedding_output_type: str = "dense"
    memory_embedding_document_template_version: int = 1
    memory_embedding_query_instruct: str = (
        "Retrieve personal memory facts relevant to the conversational query."
    )
    memory_embedding_request_timeout_seconds: float = 20.0
    memory_embedding_max_text_characters: int = 4000
    memory_embedding_worker_enabled: bool = True
    memory_embedding_worker_interval_seconds: float = 5.0
    memory_embedding_worker_claim_limit: int = 100
    memory_embedding_retry_initial_seconds: float = 30.0
    memory_embedding_http_concurrency: int = 2
    memory_embedding_query_cache_ttl_seconds: float = 600.0
    memory_embedding_query_cache_max_entries: int = 512
    memory_rebuild_enabled: bool = False
    memory_rebuild_worker_interval_seconds: float = 5.0
    memory_rebuild_scan_batch_size: int = 100
    memory_rebuild_extraction_concurrency: int = 2
    memory_rebuild_commit_batch_size: int = 20
    memory_rebuild_context_event_limit: int = 8
    memory_rebuild_retry_initial_seconds: float = 30.0
    memory_rebuild_review_page_size: int = 20
    memory_rebuild_source_excerpt_characters: int = 500
    agent_max_tool_calls: int = 32
    agent_max_model_requests: int = 24
    agent_tool_result_max_characters: int = 24000

    tooling_max_parallel_calls: int = 8
    tooling_result_token_budget: int | None = None
    tooling_result_item_limit: int | None = None
    tooling_result_artifact_enabled: bool = True
    tooling_result_artifact_retention_seconds: int = 86400

    conversation_auto_reply_enabled: bool = True
    conversation_proactive_wakeup_enabled: bool = True
    conversation_autonomous_enabled: bool = True
    conversation_semantic_participation_enabled: bool = False
    semantic_participation_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    semantic_participation_model: str = "jev-1.13.0"
    semantic_participation_state_path: Path = Path("data/participation.sqlite3")
    semantic_participation_model_config_file: Path = Path("config/autonomous-model.json")
    runtime_work_enabled: bool = False
    work_protocol_total_max_bytes: int = Field(default=2 * 1024**3, gt=0, le=2**63 - 1)
    work_protocol_object_max_bytes: int = Field(default=64 * 1024**2, gt=0, le=2**63 - 1)
    work_protocol_disk_reserve_bytes: int = Field(default=64 * 1024**2, gt=0, le=2**63 - 1)
    subagents_enabled: bool = False
    subagent_concurrency: int = Field(default=2, ge=1)
    conversation_autonomous_debounce_seconds: float = 3.0
    conversation_autonomous_admission_threshold: int = 80
    conversation_autonomous_batch_limit: int = 8
    conversation_autonomous_presence_window_seconds: int = 300
    conversation_interrupt_autonomous_on_new_message: bool = True
    conversation_effect_gate_timeout_seconds: float = Field(default=30.0, gt=0)
    conversation_history_around_before: int = Field(default=6, ge=0)
    conversation_history_around_after: int = Field(default=6, ge=0)
    reply_hard_max_messages: int = 10

    # Local in-process plugins.  Approval is API governance, not a Python sandbox.
    plugin_system_enabled: bool = False
    plugin_directory: Path = Path("plugins")
    plugin_direct_command_bindings: dict[str, str] = Field(default_factory=dict)
    plugin_hook_timeout_seconds: float = 3.0
    plugin_start_timeout_seconds: float = 10.0
    plugin_stop_timeout_seconds: float = 10.0
    plugin_background_task_limit: int = 4
    plugin_failure_disable_threshold: int = 3
    plugin_http_max_response_bytes: int = 2_097_152
    plugin_http_timeout_seconds: float = 15.0
    plugin_ai_session_max_history_messages: int = 200
    plugin_external_event_context_limit: int = 10
    plugin_external_event_context_characters: int = 6000
    plugin_external_event_summary_characters: int = 800

    web_enabled: bool = True
    web_mode: WebMode | None = None
    tavily_api_key: str = Field(default="", repr=False)
    web_search_backend: Literal["tavily", "model", "deepseek_anthropic"] = "model"
    web_search_bridge_state_path: Path = Path("data/web-search-bridge.sqlite3")
    web_search_depth: str = "advanced"
    web_search_max_results: int = 5
    web_timeout_seconds: float = 180.0
    web_max_retries: int = 1
    web_global_concurrency: int = 4
    web_source_retention_days: int = 7

    vision_enabled: bool = False
    vision_provider: str = "qwen"
    vision_base_url: str = ""
    vision_api_key: str = Field(default="", repr=False)
    vision_model: str = "qwen3.7-plus"
    vision_timeout_seconds: float = 120.0
    vision_global_concurrency: int = 4
    vision_queue_max_pending: int = 32
    vision_queue_timeout_seconds: float = 120.0
    vision_media_download_timeout_seconds: float = 120.0
    vision_allow_private_urls: bool = False
    vision_max_output_tokens: int = 8192
    vision_thinking_enabled: bool = True
    vision_thinking_budget: int = 6144
    vision_max_images_per_turn: int = 5
    vision_max_frames_per_turn: int = 16
    vision_gif_max_frames: int = 8
    vision_video_max_duration_seconds: int = 600
    vision_video_sample_interval_seconds: int = 5
    vision_video_max_frames: int = 16
    vision_video_max_download_bytes: int = 209_715_200
    vision_max_download_bytes: int = 20_971_520
    vision_max_prepared_bytes: int = 16_777_216
    vision_max_dimension: int = 4096
    vision_max_pixels: int = 16_777_216
    vision_per_user_requests_per_minute: int = 20
    vision_per_group_requests_per_minute: int = 60
    vision_analysis_retention_days: int = 7

    # Incoming speech recognition remains independent of outgoing media delivery.
    asr_enabled: bool = True
    asr_protocol: ASRProtocol = "chat_completions"
    asr_base_url: str = ""
    asr_api_key: str = Field(default="", repr=False)
    asr_model: str = "qwen3-asr-flash"
    asr_timeout_seconds: float = Field(default=60, gt=0, le=180)
    asr_max_download_bytes: int = Field(default=10_485_760, gt=0, le=20_971_520)
    asr_max_duration_seconds: int = Field(default=180, gt=0, le=300)
    asr_global_concurrency: int = Field(default=2, ge=1, le=16)
    asr_queue_max_pending: int = Field(default=8, ge=1, le=64)

    automation_enabled: bool = False
    default_timezone: str = "Asia/Shanghai"
    automation_poll_seconds: float = 2.0
    automation_lease_seconds: int = 120

    @field_validator("web_search_depth")
    @classmethod
    def _web_search_depth(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in {"basic", "advanced"}:
            raise ValueError("WEB_SEARCH_DEPTH must be basic or advanced")
        return normalized

    @field_validator("default_timezone")
    @classmethod
    def _valid_default_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("DEFAULT_TIMEZONE must be a valid IANA timezone") from exc
        return normalized

    @field_validator("memory_dream_timezone")
    @classmethod
    def _valid_memory_dream_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("MEMORY_DREAM_TIMEZONE must be a valid IANA timezone") from exc
        return normalized

    @field_validator("plugin_directory")
    @classmethod
    def _safe_plugin_directory(cls, value: Path) -> Path:
        path = Path(value)
        resolved = path.resolve(strict=False)
        sensitive = {Path(resolved.anchor), Path.home().resolve(strict=False)}
        if resolved in sensitive:
            raise ValueError("PLUGIN_DIRECTORY must not point to a filesystem root or home")
        lowered = {part.casefold() for part in resolved.parts}
        if {"windows", "system32"}.issubset(lowered):
            raise ValueError("PLUGIN_DIRECTORY must not point to a system directory")
        return path

    @field_validator("plugin_direct_command_bindings")
    @classmethod
    def _valid_direct_command_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_direct_command_bindings(value)

    @model_validator(mode="after")
    def _validate_code_worker_settings(self) -> Self:
        if self.code_mode_foreground_reserved_processes >= self.code_mode_max_worker_processes:
            raise ValueError("code worker foreground reserve must be smaller than total capacity")
        if bool(self.code_mode_launcher_path) != bool(self.code_mode_launcher_sha256):
            raise ValueError("code worker launcher path and digest must be configured together")
        return self

    @model_validator(mode="after")
    def _direct_bindings_do_not_shadow_ai_prefix(self) -> Self:
        ai_prefix = self.ai_prefix.strip()
        if not ai_prefix:
            return self
        for prefix in self.plugin_direct_command_bindings:
            if prefix.startswith(ai_prefix) or ai_prefix.startswith(prefix):
                raise ValueError(
                    f"PLUGIN_DIRECT_COMMAND_BINDINGS must not overlap AI_PREFIX: {prefix!r}"
                )
        return self

    @model_validator(mode="after")
    def _load_soul(self) -> Self:
        self.apply_soul_document(self.read_soul_document())
        return self

    def read_soul_document(self) -> SoulDocument:
        content = self.soul_file.read_bytes()
        return SoulDocument(
            content=content.decode("utf-8"),
            revision="sha256:" + hashlib.sha256(content).hexdigest(),
        )

    def apply_soul_document(self, document: SoulDocument) -> None:
        self._soul_document = document

    @property
    def soul(self) -> str:
        return self._soul_document.content

    @property
    def active_soul_revision(self) -> str:
        return self._soul_document.revision

    def apply_character_name(self, display_name: str, revision: int) -> None:
        """Publish the canonical role record after its transaction commits."""
        if revision >= self._character_revision:
            self._character_name = display_name
            self._character_revision = revision

    @property
    def character_identity(self) -> BotIdentity:
        aliases = (self._character_name, *_csv_tuple(self.bot_aliases_csv))
        return BotIdentity(
            display_name=self._character_name,
            aliases=tuple(dict.fromkeys(alias for alias in aliases if alias.strip())),
        )

    @property
    def character_prompt(self) -> str:
        identity = self.character_identity
        reference = "【角色身份】\n当前角色的名称是 " + identity.display_name + "。"
        soul = self.soul.replace("{{name}}", identity.display_name)
        return reference + ("\n\n【SOUL】\n" + soul if soul.strip() else "")

    @model_validator(mode="after")
    def _compose_domain_settings(self) -> Self:
        """Validate every environment value at its owning domain boundary."""

        _ = (
            self.app,
            self.onebot,
            self.model_runtime,
            self.conversation,
            self.plugins,
            self.memory,
            self.web,
            self.vision,
            self.asr,
            self.automation,
            self.tooling,
        )
        return self

    @model_validator(mode="before")
    @classmethod
    def _default_web_strategy(cls, value: object) -> object:
        """Enabled web uses model search unless the operator selects an external backend."""
        if isinstance(value, dict):
            supplied = {str(key).casefold() for key in value}
            if not {"web_mode", "web_enabled"}.intersection(supplied):
                value = {**value, "web_mode": WebMode.NATIVE}
            if (
                str(value.get("web_mode", value.get("WEB_MODE", ""))) == "tavily"
                and "web_search_backend" not in supplied
            ):
                value = {**value, "web_search_backend": "tavily"}
        return value

    @model_validator(mode="after")
    def _validate_embedding_settings(self) -> Self:
        if self.memory_embedding_provider != "qwen_dashscope":
            raise ValueError("MEMORY_EMBEDDING_PROVIDER must be qwen_dashscope")
        if self.memory_embedding_output_type != "dense":
            raise ValueError("MEMORY_EMBEDDING_OUTPUT_TYPE must be dense")
        if self.memory_embedding_dimensions != 1024:
            raise ValueError("qwen_dashscope currently supports 1024 dimensions")
        if self.memory_embedding_document_template_version != 1:
            raise ValueError("unsupported MEMORY_EMBEDDING_DOCUMENT_TEMPLATE_VERSION")
        return self

    @cached_property
    def app(self) -> AppSettings:
        return AppSettings.model_validate(self)

    @cached_property
    def onebot(self) -> OneBotSettings:
        return OneBotSettings.model_validate(self)

    @cached_property
    def model_runtime(self) -> ModelRuntimeSettings:
        return ModelRuntimeSettings.model_validate(self)

    @cached_property
    def conversation(self) -> ConversationSettings:
        return ConversationSettings.model_validate(self)

    @cached_property
    def plugins(self) -> PluginSettings:
        return PluginSettings.model_validate(self)

    @cached_property
    def memory(self) -> MemorySettings:
        return MemorySettings.model_validate(self)

    @cached_property
    def web(self) -> WebSettings:
        return WebSettings.model_validate(self)

    @cached_property
    def vision(self) -> VisionSettings:
        return VisionSettings.model_validate(self)

    @cached_property
    def asr(self) -> ASRSettings:
        return ASRSettings.model_validate(self)

    @property
    def asr_credentials(self) -> tuple[str, str]:
        """Reuse one complete Qwen connection, never mix a DeepSeek key and ASR URL."""
        if self.asr_base_url.strip() or self.asr_api_key.strip():
            return self.asr_base_url.strip(), self.asr_api_key.strip()
        if self.vision_provider.casefold() == "qwen":
            if self.vision_base_url.strip() and self.vision_api_key.strip():
                return self.vision_base_url.strip(), self.vision_api_key.strip()
        if "qwen" in self.llm_model.casefold():
            return self.llm_base_url.strip(), self.llm_api_key.strip()
        return "", ""

    @cached_property
    def automation(self) -> AutomationSettings:
        return AutomationSettings.model_validate(self)

    @cached_property
    def tooling(self) -> ToolingSettings:
        return ToolingSettings.model_validate(self)

    @cached_property
    def enabled_groups(self) -> frozenset[str]:
        return _csv_set(self.enabled_groups_csv)

    @cached_property
    def ignored_bot_users(self) -> frozenset[str]:
        return _csv_set(self.ignored_bot_users_csv)

    @property
    def sqlite_path(self) -> Path | None:
        """Return the local SQLite path without exposing it in user-facing output."""

        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url.removeprefix(prefix))

    @property
    def llm_configured(self) -> bool:
        """Whether enough configuration exists to make LLM requests."""

        if self.llm_provider.casefold() == "fake":
            return True
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def web_configured(self) -> bool:
        """Whether web is enabled; WebModule validates the selected catalog backend."""
        return self.web.mode is not WebMode.DISABLED

    @property
    def vision_configured(self) -> bool:
        """Whether vision is enabled with all required provider configuration."""

        return bool(
            self.vision_enabled
            and self.vision_base_url.strip()
            and self.vision_api_key.strip()
            and self.vision_model.strip()
        )

    @property
    def memory_embedding_configured(self) -> bool:
        return bool(
            self.memory_embedding_enabled
            and self.memory_embedding_base_url.strip()
            and self.memory_embedding_api_key.strip()
        )
