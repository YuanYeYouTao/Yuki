"""Resolve provider search defaults while preparing a model configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from qq_ai_bot.web.models import WebMode


def normalize_model_search_document(
    document: dict[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    web_mode: WebMode = WebMode.NATIVE,
    search_backend: str = "model",
) -> dict[str, Any]:
    """Fill missing search choices without changing explicit connections or protocols.

    Called before catalog validation and persistence, never while dispatching a
    request. Unknown gateway paths remain explicit operator configuration.
    """
    result = deepcopy(document)
    backend = result.get("search_backend") or search_backend
    if web_mode is WebMode.DISABLED or backend == "deepseek_anthropic":
        return result
    if backend == "tavily":
        result.setdefault("search_backend", "tavily")
        return result
    result["search_backend"] = "model"
    global_connection = result.get("search_connection")
    profiles = result["profiles"]
    routed = dict.fromkeys(
        result["routes"][task]
        for task in ("chat_agent", "plugin_agent_session")
        if task in result["routes"]
    )
    for profile_id in routed:
        profile = profiles.get(profile_id)
        if profile is None:
            continue  # The catalog reports unknown routes with their original identity.
        protocol = profile.get("protocol", "chat_completions")
        mode = profile.get("search_mode")
        if mode in {"native", "both"}:
            continue
        if profile.get("search_connection") or global_connection:
            if mode is None:
                profile["search_mode"] = "bridge"
            continue
        search_id = profile_id
        if profile.get("provider", "").casefold() == "deepseek" and protocol in {
            "chat_completions",
            "responses",
        }:
            endpoint = profile.get("base_url", "")
            if "base_url_env" in profile:
                name = profile["base_url_env"]
                endpoint = (environment or {}).get(name) or os.environ.get(name, "")
            address = urlsplit(endpoint)
            if address.path.rstrip("/") not in {"", "/v1"}:
                continue
            if not address.scheme or not address.netloc:
                continue
            search_id = f"{profile_id}_search"
            index = 2
            while search_id in profiles:
                search_id = f"{profile_id}_search_{index}"
                index += 1
            search = deepcopy(profile)
            for field in (
                "base_url_env",
                "wire_options",
                "search_mode",
                "search_connection",
                "thinking_mode",
                "reasoning_effort",
                "reasoning_effort_env",
            ):
                search.pop(field, None)
            search.update(
                protocol="anthropic_messages",
                base_url=urlunsplit(
                    (address.scheme, address.netloc, "/anthropic/v1", address.query, "")
                ),
                thinking_enabled=False,
                capabilities=["native_web_search"],
            )
            profiles[search_id] = search
        elif protocol not in {"gemini", "anthropic_messages", "responses"}:
            continue

        if mode is None:
            if protocol == "anthropic_messages":
                profile["search_mode"] = "native"
                profile["capabilities"] = list(
                    dict.fromkeys([*profile.get("capabilities", []), "native_web_search"])
                )
                continue
            if protocol == "responses" and search_id == profile_id:
                profile["search_mode"] = "native"
                profile["capabilities"] = list(
                    dict.fromkeys([*profile.get("capabilities", []), "native_web_search"])
                )
                continue
            profile["search_mode"] = "bridge"
        profile["search_connection"] = search_id
    return result
