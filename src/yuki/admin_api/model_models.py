"""Typed public projections of the existing model bundle and mutation receipts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, with_config
from typing_extensions import TypedDict

from yuki.admin.authority.operations import OperationRef
from yuki.domain.messages import ReasoningEffort
from yuki.llm.vendor_policy import ChatWireOptions
from yuki.model_runtime.connections import (
    ASRConnection,
    EmbeddingConnection,
    SearchConnection,
    VisionConnection,
)
from yuki.model_runtime.models import (
    ModelCapability,
    ModelProtocol,
    ModelSearchMode,
    ModelTask,
    StructuredOutputMode,
)
from yuki.model_runtime.provider_presets import EmbeddingDefaults


@with_config(ConfigDict(extra="forbid"))
class ModelProfileDocument(TypedDict, total=False):
    provider: str
    protocol: ModelProtocol
    base_url: str
    base_url_env: str
    api_key_env: str
    model: str
    model_env: str
    timeout_seconds: float
    max_retries: int
    default_temperature: float
    default_max_output_tokens: int
    max_output_tokens_limit: int | None
    max_input_tokens: int | None
    context_window_tokens: int | None
    thinking_enabled: bool | None
    thinking_mode: str
    reasoning_effort: ReasoningEffort | None
    reasoning_effort_env: str
    structured_output_mode: StructuredOutputMode
    search_mode: ModelSearchMode | None
    search_connection: str | None
    capabilities: list[ModelCapability]
    wire_options: ChatWireOptions | None


class SemanticConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    endpoint: str | None = None
    api_key_env: str | None = None
    request_model: str
    response_model: str | None = None
    timeout_seconds: float | None = None


class SemanticObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: str = ""
    connections: dict[str, SemanticConnection] = Field(default_factory=dict)


class ModelDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int
    profiles: dict[str, ModelProfileDocument]
    routes: dict[str, str]
    search_backend: Literal["model", "tavily"] | None = None
    search_connection: str | None = None
    semantic_observation: SemanticObservation | None = None
    embedding: EmbeddingConnection | None = None
    vision: VisionConnection | None = None
    asr: ASRConnection | None = None
    search: SearchConnection | None = None


class ProviderPresetView(BaseModel):
    provider: str
    display_name: str
    protocol: ModelProtocol
    default_base_url: str
    api_key_env_suggestion: str
    docs_url: str
    supports_model_listing: bool
    embedding_defaults: EmbeddingDefaults | None
    embedding_provider: Literal["qwen_dashscope", "openai_compatible"] | None
    supports_embedding: bool


class SemanticProviderPreset(BaseModel):
    provider: str
    default_endpoint: str | None
    default_api_key_env: str | None
    required: list[str]


class ResolvedModelProfile(BaseModel):
    base_url: str
    model: str
    reasoning_effort: ReasoningEffort | None


class _ModelBundleMetadata(BaseModel):
    file_id: Literal["model_profiles"]
    exists: bool
    revision: int
    apply_mode: Literal["hot_reload", "restart"]
    matches_loaded: bool | None
    semantic_matches_loaded: bool | None
    semantic_valid: bool
    semantic_providers: list[SemanticProviderPreset]
    provider_presets: list[ProviderPresetView]
    task_requirements: dict[str, list[ModelCapability]]
    writable_directory: bool


class ValidModelBundle(_ModelBundleMetadata):
    valid: Literal[True]
    document: ModelDocument
    search_backend: Literal["model", "tavily"]
    profile_schema: dict[str, JsonValue]
    tasks: list[ModelTask]
    saved_api_key_profiles: list[str]
    saved_semantic_keys: list[str]
    connection_schemas: dict[str, dict[str, JsonValue]]
    saved_connection_keys: list[str]
    resolved_profiles: dict[str, ResolvedModelProfile] | None = None


class InvalidModelBundle(_ModelBundleMetadata):
    """Invalid files can have a partial projection; they are never an editable document."""

    valid: Literal[False]
    error_category: Literal["validation_error"]
    document: JsonValue = None
    search_backend: str | None = None
    profile_schema: dict[str, JsonValue] | None = None
    tasks: list[ModelTask] | None = None
    saved_api_key_profiles: list[str] | None = None
    saved_semantic_keys: list[str] | None = None
    connection_schemas: dict[str, dict[str, JsonValue]] | None = None
    saved_connection_keys: list[str] | None = None
    resolved_profiles: dict[str, ResolvedModelProfile] | None = None


type ModelBundle = ValidModelBundle | InvalidModelBundle


class ModelControlResult(BaseModel):
    success: bool
    resource_id: str
    revision: int
    audit_id: str
    effective_state: dict[str, JsonValue]
    operation: OperationRef | None
