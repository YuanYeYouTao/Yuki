"""Explicit one-shot model connection checks; no Work, saves or invocation ledger."""

from time import perf_counter
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic.json_schema import SkipJsonSchema

from yuki.admin.config_files import ConfigFileService
from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.llm.base import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUnsupportedFeatureError,
    ProviderModel,
)
from yuki.model_runtime.models import ModelProfile, ModelProtocol
from yuki.model_runtime.pool import ModelClientPool

type ModelConnectionError = Literal[
    "authentication",
    "rate_limit",
    "quota",
    "timeout",
    "protocol",
    "unavailable",
    "configuration",
    "unsupported",
]


class SavedModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1)


class DraftModelProfile(ModelProfile):
    headers: SkipJsonSchema[dict[str, str]] = Field(default_factory=dict, repr=False)


class DraftModelTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_profile: DraftModelProfile
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _public_profile(self) -> Self:
        if "headers" in self.draft_profile.model_fields_set:
            raise ValueError("headers are not accepted through model administration")
        return self


class DraftConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)
    protocol: ModelProtocol
    base_url: str = Field(min_length=1)
    api_key_env: str = ""
    timeout_seconds: float = Field(gt=0)


class DraftModelDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_connection: DraftConnection
    api_key: SecretStr | None = None


type ModelTestInput = SavedModelConnection | DraftModelTest
type ModelDiscoverInput = SavedModelConnection | DraftModelDiscovery


class ModelTestResult(BaseModel):
    ok: bool
    latency_seconds: float
    error_category: ModelConnectionError | None = None


class ModelDiscoverResult(BaseModel):
    models: list[ProviderModel] = Field(default_factory=list)
    error_category: ModelConnectionError | None = None


def error_category(error: LLMError) -> ModelConnectionError:
    if isinstance(error, LLMAuthenticationError):
        return "authentication"
    if isinstance(error, LLMRateLimitError):
        if error.diagnostics.get("code") == "insufficient_quota":
            return "quota"
        return "rate_limit"
    if isinstance(error, LLMTimeoutError):
        return "timeout"
    if isinstance(error, LLMUnsupportedFeatureError):
        return "unsupported"
    if isinstance(error, LLMConfigurationError):
        return "configuration"
    if isinstance(error, LLMUnavailableError):
        return "unavailable"
    return "protocol"


async def test_connection(files: ConfigFileService, source: ModelTestInput) -> ModelTestResult:
    started = perf_counter()
    selected = (
        source.profile_id if isinstance(source, SavedModelConnection) else source.draft_profile
    )
    profile, secrets = await files.model_connection(selected)
    if isinstance(source, DraftModelTest) and source.api_key is not None:
        secrets[profile.api_key_env] = source.api_key.get_secret_value()
    pool = ModelClientPool(secret_overrides=secrets)
    try:
        provider = pool.get(profile.model_copy(update={"max_retries": 0}))
        maximum = min(16, profile.default_max_output_tokens)
        if profile.max_output_tokens_limit is not None:
            maximum = min(maximum, profile.max_output_tokens_limit)
        await provider.complete(
            ChatRequest(
                messages=(ChatMessage(role="user", content="Reply OK."),),
                model=profile.model,
                max_output_tokens=maximum,
                thinking_enabled=False,
            )
        )
        return ModelTestResult(ok=True, latency_seconds=perf_counter() - started)
    except LLMError as exc:
        return ModelTestResult(
            ok=False, latency_seconds=perf_counter() - started, error_category=error_category(exc)
        )
    finally:
        await pool.close()


async def discover_models(
    files: ConfigFileService, source: ModelDiscoverInput
) -> ModelDiscoverResult:
    if isinstance(source, SavedModelConnection):
        selected: ModelProfile | str = source.profile_id
    else:
        selected = ModelProfile(
            id="discovery",
            model="discovery",
            max_retries=0,
            default_temperature=0,
            default_max_output_tokens=1,
            **source.draft_connection.model_dump(),
        )
    profile, secrets = await files.model_connection(selected)
    if isinstance(source, DraftModelDiscovery) and source.api_key is not None:
        secrets[profile.api_key_env] = source.api_key.get_secret_value()
    pool = ModelClientPool(secret_overrides=secrets)
    try:
        return ModelDiscoverResult(models=await pool.get(profile).list_models())
    except LLMError as exc:
        return ModelDiscoverResult(error_category=error_category(exc))
    finally:
        await pool.close()
