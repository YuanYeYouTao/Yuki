"""Explicit vendor wire settings, independent of business tasks and message content."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatWireOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning: Literal[
        "effort", "thinking", "enable_thinking", "openrouter", "builtin", "gemini", "budget"
    ] = "effort"
    token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    send_temperature: bool = False
    send_tool_choice: bool = True
    replay_reasoning: bool = True
    reasoning_split: bool = False
    send_reasoning_effort: bool = False
    native_web_search: bool = False
    thinking_budget_tokens: int = Field(default=4096, ge=1024)
    include_reasoning: bool | None = None
    reasoning_format: Literal["parsed", "hidden"] | None = None


# Vendor presets specify wire dialect, not claims about any particular model.
CHAT_VENDORS = frozenset(
    {
        "openai",
        "openai_compatible",
        "deepseek",
        "qwen",
        "moonshot",
        "zhipu",
        "doubao",
        "minimax",
        "openrouter",
        "siliconflow",
        "together",
        "groq",
        "mistral",
        "xai",
        "azure_openai",
    }
)
RESPONSES_VENDORS = frozenset({"openai", "openai_compatible", "deepseek"})


def wire_options(vendor: str, overrides: ChatWireOptions | None = None) -> ChatWireOptions:
    defaults: dict[str, object] = {}
    if vendor in {"openai", "azure_openai"}:
        defaults = {"token_field": "max_completion_tokens", "replay_reasoning": False}
    elif vendor == "deepseek":
        defaults = {
            "reasoning": "thinking",
            "send_tool_choice": False,
            "send_reasoning_effort": True,
        }
    elif vendor == "qwen":
        defaults = {"reasoning": "enable_thinking"}
    elif vendor in {"moonshot", "zhipu", "doubao"}:
        defaults = {"reasoning": "thinking"}
        if vendor == "doubao":
            defaults["send_reasoning_effort"] = True
    elif vendor == "minimax":
        defaults = {"reasoning": "builtin", "reasoning_split": True}
    elif vendor == "openrouter":
        defaults = {"reasoning": "openrouter"}
    elif vendor == "groq":
        defaults = {
            "token_field": "max_completion_tokens",
            "include_reasoning": True,
        }
    elif vendor == "mistral":
        defaults = {}
    elif vendor == "anthropic":
        defaults = {"reasoning": "effort"}
    elif vendor == "gemini":
        defaults = {"reasoning": "gemini"}
    if overrides is not None:
        defaults.update(overrides.model_dump(exclude_unset=True))
    return ChatWireOptions.model_validate(defaults)


def supports_native_search(
    vendor: str, protocol: str, options: ChatWireOptions | None, *, has_functions: bool
) -> bool:
    if protocol == "responses":
        return vendor != "deepseek"
    if protocol == "gemini":
        # Gemini GenerateContent can combine Google Search with function declarations.
        return True
    if protocol == "anthropic_messages":
        return True
    if protocol == "chat_completions":
        return wire_options(vendor, options).native_web_search and not has_functions
    return False
