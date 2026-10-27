"""Semantic observation connections stored beside the chat catalog.

The observer sends the saved endpoint and model names. Provider ids only choose
which preset may fill an omitted endpoint; they do not select a protocol.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from yuki.model_runtime.secrets import KEY_ALIAS

PROVIDER_IDS = ("jev", "tencent_jev", "aliyun_decision", "custom")
PRESET_ENDPOINTS = {
    "jev": "https://api.typesafe.ai/v1/systemone",
    "tencent_jev": "https://ai-gateway.edgeone.link/v1/systemone",
}
DEFAULT_KEY_ENV = {
    "jev": "SEMANTIC_PARTICIPATION_API_KEY",
    "tencent_jev": "YUKI_SEMANTIC_TENCENT_JEV_API_KEY",
    "aliyun_decision": "YUKI_SEMANTIC_ALIYUN_DECISION_API_KEY",
}
ENVIRONMENT_CONNECTION_NAME = "typesafe"
ENVIRONMENT_KEY_ENV = "SEMANTIC_PARTICIPATION_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 12.0
UNLOADED: Any = object()
INVALID: Any = object()

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_CONNECTION_FIELDS = {
    "provider",
    "endpoint",
    "api_key_env",
    "request_model",
    "response_model",
    "timeout_seconds",
}
_SECTION_FIELDS = {"route", "connections"}


class SemanticConfigError(ValueError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def provider_catalog() -> list[dict[str, Any]]:
    """Response-only catalog. It is not written into the TOML."""

    rows: list[dict[str, Any]] = []
    for provider in PROVIDER_IDS:
        required = ["request_model"]
        if provider in {"aliyun_decision", "custom"}:
            required.append("endpoint")
        if provider == "custom":
            required.append("api_key_env")
        rows.append(
            {
                "provider": provider,
                "default_endpoint": PRESET_ENDPOINTS.get(provider),
                "default_api_key_env": DEFAULT_KEY_ENV.get(provider),
                "required": required,
            }
        )
    return rows


def normalize_semantic_section(raw: Any) -> dict[str, Any] | None:
    """Return the public section, or None when the operator cleared it."""

    if raw is None or raw == {}:
        return None
    if not isinstance(raw, Mapping):
        raise SemanticConfigError("semantic_connection_invalid")
    if not set(raw).issubset(_SECTION_FIELDS):
        raise SemanticConfigError("semantic_connection_invalid")
    route = raw.get("route", "")
    if route is None:
        route = ""
    if not isinstance(route, str) or (route and _NAME.fullmatch(route) is None):
        raise SemanticConfigError("semantic_connection_invalid")
    connections = raw.get("connections", {})
    if connections is None:
        connections = {}
    if not isinstance(connections, Mapping):
        raise SemanticConfigError("semantic_connection_invalid")
    if route == "" and not connections:
        return None
    parsed: dict[str, dict[str, Any]] = {}
    for name, item in connections.items():
        if (
            not isinstance(name, str)
            or _NAME.fullmatch(name) is None
            or not isinstance(item, Mapping)
        ):
            raise SemanticConfigError("semantic_connection_invalid")
        parsed[name] = _normalize_connection(item)
    return {"route": route, "connections": parsed}


def take_semantic_submission(
    document: dict[str, Any], existing: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, Any, bool]:
    """Split a model save into the runtime section, the value to persist, and usability.

    An omitted section keeps the on-disk value. An explicit empty section deletes it
    and returns the process to the environment connection. A submitted section is
    stored in its normalized public form.
    """

    if "semantic_observation" in document:
        section = normalize_semantic_section(document.pop("semantic_observation"))
        return section, section, True
    if "semantic_observation" not in existing:
        return None, None, True
    raw = existing["semantic_observation"]
    try:
        return normalize_semantic_section(raw), raw, True
    except SemanticConfigError:
        return None, raw, False


def semantic_key_envs(section: Mapping[str, Any] | None) -> set[str]:
    if not section:
        return set()
    connections = section.get("connections", {})
    if not isinstance(connections, Mapping):
        return set()
    return {
        item["api_key_env"]
        for item in connections.values()
        if isinstance(item, Mapping) and isinstance(item.get("api_key_env"), str)
    }


def semantic_use_document(
    document: Mapping[str, Any],
    *,
    provider: str,
    request_model: str,
    name: str | None = None,
    endpoint: str | None = None,
    response_model: str | None = None,
    timeout_seconds: float | None = None,
    key_env: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Replace one named connection and select it, leaving the chat catalog untouched."""

    if provider not in PROVIDER_IDS:
        raise SemanticConfigError("semantic_provider_unknown")
    connection_name = name or provider
    raw_connection: dict[str, Any] = {"provider": provider, "request_model": request_model}
    if endpoint:
        raw_connection["endpoint"] = endpoint
    if response_model:
        raw_connection["response_model"] = response_model
    if timeout_seconds is not None:
        raw_connection["timeout_seconds"] = timeout_seconds
    if key_env:
        raw_connection["api_key_env"] = key_env
    current = document.get("semantic_observation")
    connections: dict[str, Any] = {}
    if isinstance(current, Mapping) and isinstance(current.get("connections"), Mapping):
        connections = dict(current["connections"])
    connections[connection_name] = raw_connection
    section = normalize_semantic_section({"route": connection_name, "connections": connections})
    if section is None or connection_name not in section["connections"]:
        raise SemanticConfigError("semantic_connection_invalid")
    updated = dict(document)
    updated["semantic_observation"] = section
    return updated, section["connections"][connection_name]["api_key_env"]


def connection_revision(
    *, endpoint: str, request_model: str, response_model: str, api_key: str
) -> str:
    """Identify the connection without storing its secret."""

    fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    material = "\n".join((endpoint, request_model, response_model, fingerprint))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def runtime_connection(
    section: Mapping[str, Any] | None,
    *,
    environment_model: str,
    lookup: Callable[[str], str],
) -> dict[str, Any] | None:
    """Resolve the one connection new observations may use.

    No section uses the process environment. A section that does not resolve, or
    whose key is empty, produces no connection and does not fall back.
    """

    if section is None:
        model = environment_model.strip()
        key = lookup(ENVIRONMENT_KEY_ENV)
        if not key or not model:
            return None
        endpoint = PRESET_ENDPOINTS["jev"]
        return _runtime_fields(
            endpoint=endpoint,
            api_key=key,
            request_model=model,
            response_model=model,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            name=ENVIRONMENT_CONNECTION_NAME,
        )
    route = section.get("route") or ""
    connections = section.get("connections", {})
    item = connections.get(route) if isinstance(connections, Mapping) else None
    if not isinstance(route, str) or not isinstance(item, Mapping):
        return None
    key_env = item.get("api_key_env")
    if not isinstance(key_env, str):
        return None
    key = lookup(key_env)
    if not key:
        return None
    return _runtime_fields(
        endpoint=str(item["endpoint"]),
        api_key=key,
        request_model=str(item["request_model"]),
        response_model=str(item["response_model"]),
        timeout_seconds=float(item["timeout_seconds"]),
        name=route,
    )


def _normalize_connection(item: Mapping[str, Any]) -> dict[str, Any]:
    if not set(item).issubset(_CONNECTION_FIELDS):
        raise SemanticConfigError("semantic_connection_invalid")
    provider = item.get("provider")
    if provider not in PROVIDER_IDS:
        raise SemanticConfigError("semantic_provider_unknown")
    request_model = item.get("request_model")
    if not isinstance(request_model, str) or not request_model.strip():
        raise SemanticConfigError("semantic_request_model_required")
    response_model = item.get("response_model", request_model)
    if response_model is None or response_model == "":
        response_model = request_model
    if not isinstance(response_model, str) or not response_model.strip():
        raise SemanticConfigError("semantic_connection_invalid")
    endpoint = item.get("endpoint")
    if endpoint is None or (isinstance(endpoint, str) and not endpoint.strip()):
        endpoint = PRESET_ENDPOINTS.get(provider)
        if endpoint is None:
            raise SemanticConfigError("semantic_endpoint_required")
    elif not isinstance(endpoint, str):
        raise SemanticConfigError("semantic_connection_invalid")
    key_env = item.get("api_key_env")
    if key_env is None or key_env == "":
        key_env = DEFAULT_KEY_ENV.get(provider)
        if key_env is None:
            raise SemanticConfigError("semantic_key_env_required")
    if not isinstance(key_env, str) or KEY_ALIAS.fullmatch(key_env) is None:
        raise SemanticConfigError("semantic_connection_invalid")
    timeout = item.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise SemanticConfigError("semantic_timeout_invalid")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SemanticConfigError("semantic_timeout_invalid")
    return {
        "provider": provider,
        "endpoint": endpoint,
        "api_key_env": key_env,
        "request_model": request_model,
        "response_model": response_model,
        "timeout_seconds": timeout_seconds,
    }


def _runtime_fields(
    *,
    endpoint: str,
    api_key: str,
    request_model: str,
    response_model: str,
    timeout_seconds: float,
    name: str,
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "api_key": api_key,
        "request_model": request_model,
        "response_model": response_model,
        "timeout_seconds": timeout_seconds,
        "name": name,
        "revision": connection_revision(
            endpoint=endpoint,
            request_model=request_model,
            response_model=response_model,
            api_key=api_key,
        ),
    }
