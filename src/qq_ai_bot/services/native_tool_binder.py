"""Bind provider-native tools only from backend-authorized runtime state."""

from __future__ import annotations

import logging

from qq_ai_bot.domain.messages import NativeToolDefinition, NativeToolType
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProtocol, ModelSearchMode
from qq_ai_bot.web.models import WebMode

logger = logging.getLogger(__name__)


class NativeToolBinder:
    """Small provider-neutral policy intersection for native tools."""

    def bind(
        self,
        *,
        protocol: ModelProtocol,
        capabilities: frozenset[ModelCapability],
        allowed_capabilities: frozenset[str],
        web_mode: WebMode,
        web_was_used: bool,
        search_mode: ModelSearchMode | None = None,
    ) -> tuple[NativeToolDefinition, ...]:
        del web_was_used
        web_approved = bool({"web", "web_search"}.intersection(allowed_capabilities))
        if not web_approved or web_mode is WebMode.DISABLED:
            return ()
        if search_mode in {ModelSearchMode.EXTERNAL, ModelSearchMode.BRIDGE}:
            return ()
        if search_mode is None and (
            web_mode is WebMode.TAVILY
            or (protocol is ModelProtocol.ANTHROPIC_MESSAGES and web_mode is not WebMode.NATIVE)
        ):
            # Existing profiles keep their pre-migration global search choice.
            return ()
        # Explicit native/both choices are per connection. The deployment mode
        # still controls whether web is enabled at all.
        if protocol not in {
            ModelProtocol.RESPONSES,
            ModelProtocol.CHAT_COMPLETIONS,
            ModelProtocol.GEMINI,
            ModelProtocol.ANTHROPIC_MESSAGES,
        }:
            logger.warning(
                "native_tool_binding_skipped reason=protocol web_mode=%s protocol=%s",
                web_mode.value,
                protocol.value,
            )
            return ()
        if protocol is ModelProtocol.CHAT_COMPLETIONS and ModelCapability.TOOLS in capabilities:
            # Chat search models cannot honor the Main Agent's fixed function contract.
            return ()
        if ModelCapability.NATIVE_WEB_SEARCH not in capabilities:
            logger.warning(
                "native_tool_binding_skipped reason=capability web_mode=%s protocol=%s",
                web_mode.value,
                protocol.value,
            )
            return ()
        return (NativeToolDefinition(type=NativeToolType.WEB_SEARCH),)

    def excluded_function_names(
        self,
        *,
        protocol: ModelProtocol,
        capabilities: frozenset[ModelCapability],
        allowed_capabilities: frozenset[str],
        web_mode: WebMode,
        search_mode: ModelSearchMode | None = None,
    ) -> frozenset[str]:
        """Remove only a same-name local tool that would collide on the wire."""
        if search_mode is ModelSearchMode.NATIVE:
            # Native-only excludes external search and page-reading functions,
            # even when this request has no web authorization.
            return frozenset({"web_search", "read_webpage"})
        if protocol is not ModelProtocol.ANTHROPIC_MESSAGES:
            return frozenset()
        if self.bind(
            protocol=protocol,
            capabilities=capabilities,
            allowed_capabilities=allowed_capabilities,
            web_mode=web_mode,
            web_was_used=False,
            search_mode=search_mode,
        ):
            return frozenset({"web_search"})
        return frozenset()
