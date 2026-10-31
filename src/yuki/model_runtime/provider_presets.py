"""Read-only connection presets shared by administration and deployment setup.

These are connection defaults, not model capabilities or routing decisions.
An existing connection keeps its explicit endpoint, protocol and credentials.
"""

from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from yuki.llm.vendor_policy import RESPONSES_VENDORS
from yuki.model_runtime.profiles import _DEFAULT_REQUIREMENTS


@dataclass(frozen=True, slots=True)
class EmbeddingDefaults:
    provider: Literal["qwen_dashscope", "openai_compatible"]
    base_url: str


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    provider: str
    display_name: str
    protocol: str
    default_base_url: str
    api_key_env_suggestion: str
    docs_url: str
    supports_model_listing: bool
    embedding_defaults: EmbeddingDefaults | None = None


_CHAT_PRESETS = (
    ProviderPreset(
        "openai",
        "OpenAI",
        "chat_completions",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        "https://developers.openai.com/api/reference/overview",
        True,
        EmbeddingDefaults("openai_compatible", "https://api.openai.com/v1"),
    ),
    ProviderPreset(
        "deepseek",
        "DeepSeek",
        "chat_completions",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        "https://api-docs.deepseek.com/",
        True,
    ),
    ProviderPreset(
        "qwen",
        "Qwen / 阿里雲百鍊",
        "chat_completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        "https://help.aliyun.com/zh/model-studio/getting-started/first-api-call-to-qwen",
        True,
        EmbeddingDefaults("qwen_dashscope", "https://dashscope.aliyuncs.com/api/v1"),
    ),
    ProviderPreset(
        "moonshot",
        "Moonshot / Kimi",
        "chat_completions",
        "https://api.moonshot.ai/v1",
        "MOONSHOT_API_KEY",
        "https://platform.moonshot.ai/docs/",
        True,
    ),
    ProviderPreset(
        "zhipu",
        "智譜 GLM",
        "chat_completions",
        "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPU_API_KEY",
        "https://docs.bigmodel.cn/",
        False,
    ),
    ProviderPreset(
        "doubao",
        "豆包 / 火山方舟",
        "chat_completions",
        "https://ark.cn-beijing.volces.com/api/v3",
        "ARK_API_KEY",
        "https://docs.volcengine.com/docs/ark/base-url-and-authentication",
        False,
    ),
    ProviderPreset(
        "minimax",
        "MiniMax",
        "chat_completions",
        "https://api.minimax.io/v1",
        "MINIMAX_API_KEY",
        "https://platform.minimax.io/docs/api-reference/text-openai-api",
        False,
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "chat_completions",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/docs/quickstart",
        True,
    ),
    ProviderPreset(
        "siliconflow",
        "SiliconFlow / 矽基流動",
        "chat_completions",
        "https://api.siliconflow.cn/v1",
        "SILICONFLOW_API_KEY",
        "https://api-docs.siliconflow.cn/",
        True,
        EmbeddingDefaults("openai_compatible", "https://api.siliconflow.cn/v1"),
    ),
    ProviderPreset(
        "together",
        "Together AI",
        "chat_completions",
        "https://api.together.xyz/v1",
        "TOGETHER_API_KEY",
        "https://docs.together.ai/docs/openai-api-compatibility",
        True,
        EmbeddingDefaults("openai_compatible", "https://api.together.xyz/v1"),
    ),
    ProviderPreset(
        "groq",
        "Groq",
        "chat_completions",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "https://console.groq.com/docs/overview",
        True,
    ),
    ProviderPreset(
        "mistral",
        "Mistral AI",
        "chat_completions",
        "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY",
        "https://docs.mistral.ai/api/",
        True,
        EmbeddingDefaults("openai_compatible", "https://api.mistral.ai/v1"),
    ),
    ProviderPreset(
        "xai",
        "xAI",
        "chat_completions",
        "https://api.x.ai/v1",
        "XAI_API_KEY",
        "https://docs.x.ai/docs/overview",
        True,
    ),
    ProviderPreset(
        "azure_openai",
        "Azure OpenAI",
        "chat_completions",
        "",
        "AZURE_OPENAI_API_KEY",
        "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle",
        True,
        EmbeddingDefaults("openai_compatible", ""),
    ),
    ProviderPreset(
        "openai_compatible",
        "自訂（OpenAI 相容）",
        "chat_completions",
        "",
        "YUKI_MODEL_API_KEY",
        "https://developers.openai.com/api/reference/overview",
        True,
        EmbeddingDefaults("openai_compatible", ""),
    ),
)

PROVIDER_PRESETS = (
    *_CHAT_PRESETS,
    *(
        replace(preset, protocol="responses", display_name=f"{preset.display_name} · Responses")
        for preset in _CHAT_PRESETS
        if preset.provider in RESPONSES_VENDORS
    ),
    ProviderPreset(
        "anthropic",
        "Anthropic / Claude",
        "anthropic_messages",
        "https://api.anthropic.com/v1",
        "ANTHROPIC_API_KEY",
        "https://platform.claude.com/docs/en/api/overview",
        True,
    ),
    ProviderPreset(
        "gemini",
        "Google Gemini",
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta",
        "GEMINI_API_KEY",
        "https://ai.google.dev/gemini-api/docs",
        True,
    ),
    ProviderPreset(
        "anthropic",
        "自訂（Claude Messages）",
        "anthropic_messages",
        "",
        "YUKI_MODEL_API_KEY",
        "https://platform.claude.com/docs/en/api/overview",
        True,
    ),
    ProviderPreset(
        "gemini",
        "自訂（Gemini）",
        "gemini",
        "",
        "YUKI_MODEL_API_KEY",
        "https://ai.google.dev/gemini-api/docs",
        True,
    ),
    ProviderPreset(
        "openai_compatible",
        "本機（Ollama）",
        "chat_completions",
        "http://localhost:11434/v1",
        "",
        "https://docs.ollama.com/api/openai-compatibility",
        True,
        EmbeddingDefaults("openai_compatible", "http://localhost:11434/v1"),
    ),
    ProviderPreset(
        "openai_compatible",
        "本機（LM Studio）",
        "chat_completions",
        "http://localhost:1234/v1",
        "",
        "https://lmstudio.ai/docs/developer/openai-compat",
        True,
        EmbeddingDefaults("openai_compatible", "http://localhost:1234/v1"),
    ),
)


def provider_presets() -> list[dict[str, Any]]:
    """Return public metadata without exposing mutable shared dictionaries."""
    result = []
    for preset in PROVIDER_PRESETS:
        item = asdict(preset)
        item["embedding_provider"] = (
            preset.embedding_defaults.provider if preset.embedding_defaults is not None else None
        )
        item["supports_embedding"] = preset.embedding_defaults is not None
        result.append(item)
    return result


def task_requirements() -> dict[str, list[str]]:
    return {
        **{
            task.value: sorted(capability.value for capability in required)
            for task, required in _DEFAULT_REQUIREMENTS.items()
        },
        "embedding": [],
        "vision": ["image_input"],
        "asr": [],
        "search": ["native_web_search"],
    }
