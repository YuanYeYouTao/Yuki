"""Manual synthetic cache experiment. Importing this module never sends requests.

The live entry point is explicitly opt-in, outside pytest discovery and CI. It
uses the real model executor, provider serializer, and fixed core manifest,
but never starts an application lifecycle or invokes a business tool backend.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from tools.cache_probe_runtime import OrdinaryDriver, WorkDriver

from qq_ai_bot.config import Settings
from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ChatRequest, ChatTool
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.mcp.repository import ToolArtifactRepository
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelProfile, ModelProtocol, ModelRoute, ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog, parse_model_profile_catalog
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.model_runtime.secrets import read_model_secrets
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_compaction import CompactionSummary, summary_json_text
from qq_ai_bot.services.ordinary_compaction import OrdinarySummary
from qq_ai_bot.services.turn_transcript import TurnTranscript

SCENARIOS = tuple(f"C{number:02}" for number in range(1, 9))
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jf1sAAAAASUVORK5CYII="


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def endpoint_origin(value: str) -> str:
    parsed = urlsplit(value)
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def route_from_stdin(stream: Any) -> tuple[ModelProfile, str]:
    """A private pipe avoids command-line secrets and persistent credentials."""
    raw = json.load(stream)
    profile = ModelProfile.model_validate(raw["profile"])
    key = raw["api_key"]
    if not isinstance(key, str) or not key:
        raise ValueError("selected profile credential is unavailable")
    return profile, key


def summary_shape(content: str, *, ordinary: bool = False) -> dict[str, Any]:
    """Shape-only diagnosis; generated text and unknown field names stay private."""
    schema = OrdinarySummary if ordinary else CompactionSummary
    normalized = summary_json_text(content)
    shape: dict[str, Any] = {
        "schema_contract": "ordinary" if ordinary else "work",
        "format_unwrapped": normalized != content,
        "characters": len(content),
        "code_fence": content.lstrip().startswith("```"),
    }
    try:
        value = json.loads(content)
    except ValueError:
        shape["valid_json"] = False
    else:
        shape["valid_json"] = True
        shape["top_level_type"] = type(value).__name__
        if isinstance(value, dict):
            shape["field_count"] = len(value)
            shape["schema_fields_present"] = len(set(value) & set(schema.model_fields))
            shape["other_fields"] = len(set(value) - set(schema.model_fields))
    try:
        schema.model_validate_json(normalized)
        shape["schema_valid"] = True
    except ValidationError as error:
        shape["schema_valid"] = False
        allowed = set(schema.model_fields) | {
            "text",
            "refs",
            "input_ref",
            "kind",
            "reason",
            "directive_id",
        }
        shape["validation_errors"] = [
            {
                "loc": [
                    part if isinstance(part, int) or part in allowed else "<other-field>"
                    for part in item["loc"]
                ],
                "type": item["type"],
            }
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        ]
    return shape


def summary_reference_shape(content: str, source: dict[str, Any]) -> dict[str, Any]:
    """Synthetic provenance only; never retain generated text or arbitrary refs."""

    def safe(ref: str) -> str:
        if re.fullmatch(r"goal|(?:input|event|record|observation):[0-9]+", ref):
            return ref
        return "<ref-sha256:" + digest(ref) + ">"

    supplied = set(source.get("source_refs", ()))
    original = source.get("original_request_ref")
    rows: dict[str, Any] = {
        "source_refs": sorted(safe(ref) for ref in supplied),
        "original_request_ref": safe(original) if isinstance(original, str) else None,
        "source_kinds": {
            "goal": "immutable_goal",
            "original_request": "original_request_ref",
            "input": "task_inputs/recent_task_inputs",
            "record": "records/source_fragments",
            "observation": "model_observations",
            "effect": "effects",
        },
        "paging": source.get("paging", {}).get("cursor"),
        "task_input_refs": [f"input:{item['input_id']}" for item in source.get("task_inputs", ())],
        "prior_directive_refs": [
            [safe(ref) for ref in item["refs"]]
            for item in source.get("task_material", {}).get("directives", ())
        ],
        "record_sources": [
            {
                "record_ref": f"record:{index}",
                "source_ref": safe(record["source_ref"])
                if isinstance(record.get("source_ref"), str)
                else None,
                "original_request_event_id": record.get("original_request_event_id"),
            }
            for index, record in zip(
                source.get("record_source_indices", ()), source.get("records", ()), strict=True
            )
            if isinstance(record, dict)
        ],
    }
    try:
        value = CompactionSummary.model_validate_json(summary_json_text(content)).model_dump()
    except ValueError:
        rows["schema_valid"] = False
        return rows
    rows["schema_valid"] = True
    rows["input_dispositions"] = [
        {
            "input_ref": safe(item["input_ref"]),
            "kind": item["kind"],
            "reason_nonempty": bool(item["reason"].strip()),
            "supplied_task_input": item["input_ref"]
            in {f"input:{row['input_id']}" for row in source.get("task_inputs", ())},
        }
        for item in value["input_dispositions"]
    ]
    rows["sections"] = {
        section: [[safe(ref) for ref in item["refs"]] for item in value[section]]
        for section in (
            "task_directives",
            "superseded_directives",
            "completed",
            "pending",
            "failures",
            "artifacts",
            "next_steps",
        )
    }
    rows["outside_supplied_refs"] = sorted(
        {
            safe(ref)
            for section in rows["sections"]
            for item in value[section]
            for ref in item["refs"]
            if ref not in supplied
        }
    )
    rows["non_directive_source_refs"] = sorted(
        {
            safe(ref)
            for item in value["task_directives"]
            for ref in item["refs"]
            if ref != "goal" and ref != original and not ref.startswith("input:")
        }
    )
    return rows


def load_manifest(path: Path) -> tuple[ChatTool, ...]:
    """Use a separately exported frozen deployment declaration without loading plugins."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw["tools"] if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError("frozen manifest must contain tools")
    tools = tuple(
        ChatTool(name=row["name"], description=row["description"], parameters=row["parameters"])
        for row in rows
    )
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError("duplicate frozen manifest tool")
    return tools


def first_difference(left: Any, right: Any, location: str = "$") -> str | None:
    """Return positions, never the differing content or opaque signatures."""
    if type(left) is not type(right):
        return location
    if isinstance(left, dict):
        if list(left) != list(right):
            return location + ".<keys/order>"
        for key in left:
            result = first_difference(left[key], right[key], location + "." + key)
            if result is not None:
                return result
    elif isinstance(left, list):
        for index, (old, new) in enumerate(zip(left, right, strict=False)):
            result = first_difference(old, new, f"{location}[{index}]")
            if result is not None:
                return result
        if len(left) != len(right):
            return f"{location}[{min(len(left), len(right))}]"
    elif left != right:
        return location
    return None


def input_units(payload: dict[str, Any]) -> list[object]:
    # Consecutive Gemini user messages coalesce: compare actual ordered parts.
    if "contents" not in payload:
        items = payload.get("messages", payload.get("input", []))
        return items if isinstance(items, list) else [items]
    return [
        [row.get("role"), part]
        for row in payload.get("contents", [])
        for part in row.get("parts", [])
    ]


def shared_json_bytes(left: object, right: object) -> int:
    old = json.dumps(left, ensure_ascii=False, separators=(",", ":")).encode()
    new = json.dumps(right, ensure_ascii=False, separators=(",", ":")).encode()
    return next(
        (index for index, pair in enumerate(zip(old, new, strict=False)) if pair[0] != pair[1]),
        min(len(old), len(new)),
    )


def prefix_comparison(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    old, new = input_units(previous), input_units(current)
    count = 0
    size = 0
    for left, right in zip(old, new, strict=False):
        if left != right:
            break
        count += 1
        size += len(json.dumps(left, ensure_ascii=False, separators=(",", ":")).encode())
    input_keys = {"contents", "messages", "input"}
    old_fixed = {key: value for key, value in previous.items() if key not in input_keys}
    new_fixed = {key: value for key, value in current.items() if key not in input_keys}
    return {
        "body_common_json_bytes": shared_json_bytes(previous, current),
        "input_common_json_bytes": shared_json_bytes(old, new),
        "equal_input_parts": count,
        "equal_input_part_bytes": size,
        "previous_input_parts": len(old),
        "current_input_parts": len(new),
        "input_first_difference": first_difference(old, new, "$.input_parts"),
        "fixed_first_difference": first_difference(old_fixed, new_fixed, "$.fixed"),
    }


def weighted_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    known = [
        row
        for row in rows
        if type(row.get("prompt_tokens")) is int
        and row["prompt_tokens"] > 0
        and type(row.get("cached_prompt_tokens")) is int
        and 0 <= row["cached_prompt_tokens"] <= row["prompt_tokens"]
    ]
    incoming = sum(row["prompt_tokens"] for row in known)
    cached = sum(row["cached_prompt_tokens"] for row in known)
    return {
        "requests": len(rows),
        "reported_input_tokens": sum(
            row["prompt_tokens"]
            for row in rows
            if type(row.get("prompt_tokens")) is int and row["prompt_tokens"] >= 0
        ),
        "reported_output_tokens": sum(
            row["completion_tokens"]
            for row in rows
            if type(row.get("completion_tokens")) is int and row["completion_tokens"] >= 0
        ),
        "reported_total_tokens": sum(
            row["total_tokens"]
            for row in rows
            if type(row.get("total_tokens")) is int and row["total_tokens"] >= 0
        ),
        "cache_known": len(known),
        "cache_unknown_or_invalid": len(rows) - len(known),
        "positive": sum(row["cached_prompt_tokens"] > 0 for row in known),
        "explicit_zero": sum(row["cached_prompt_tokens"] == 0 for row in known),
        "known_input_tokens": incoming,
        "cached_input_tokens": cached,
        "weighted_cached_input_ratio": cached / incoming if incoming else None,
        "formula": "cached_prompt_tokens / prompt_tokens (reported input includes cache)",
    }


class PhysicalRequests:
    """Each transport attempt is retained, including failures and parse failures."""

    def __init__(self, protocol: ModelProtocol = ModelProtocol.GEMINI) -> None:
        self.protocol = protocol
        self.rows: list[dict[str, Any]] = []
        self.payloads: list[dict[str, Any]] = []
        self.sample: dict[str, Any] = {}
        self._started: dict[int, float] = {}
        self._requests: dict[int, dict[str, Any]] = {}

    async def request(self, request: httpx.Request) -> None:
        payload = json.loads(request.content)
        previous = self.payloads[-1] if self.payloads else None
        lane = self.sample.get("stage") == "auxiliary_compaction"
        previous_lane = next(
            (
                index
                for index in range(len(self.rows) - 1, -1, -1)
                if self.rows[index].get("scenario") == self.sample.get("scenario")
                and (self.rows[index].get("stage") == "auxiliary_compaction") == lane
            ),
            None,
        )
        row = {
            **self.sample,
            "physical_index": len(self.rows) + 1,
            "prepared_body_bytes": len(request.content),
            "endpoint_origin": endpoint_origin(str(request.url)),
            "endpoint_path": request.url.path,
            "tools_sha256": digest(payload.get("tools")),
            "tools_json_bytes": len(
                json.dumps(payload.get("tools"), ensure_ascii=False, separators=(",", ":")).encode()
            ),
            "body_sha256": hashlib.sha256(request.content).hexdigest(),
            "fixed_sha256": digest(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"contents", "messages", "input"}
                }
            ),
            "status_code": None,
            "prompt_tokens": None,
            "cached_prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "cache_creation_input_tokens": None,
            "unknown_usage": True,
            "response_format_type": (payload.get("response_format") or {}).get("type")
            if isinstance(payload.get("response_format"), dict)
            else None,
            "gemini_response_schema_present": isinstance(
                (payload.get("generationConfig") or {}).get("responseJsonSchema"), dict
            ),
            "comparison": prefix_comparison(previous, payload) if previous else None,
            "comparison_previous_stage": self.rows[-1].get("stage") if self.rows else None,
            "same_lane_previous_index": previous_lane + 1 if previous_lane is not None else None,
            "same_lane_comparison": prefix_comparison(self.payloads[previous_lane], payload)
            if previous_lane is not None
            else None,
        }
        self.rows.append(row)
        self.payloads.append(payload)
        self._requests[id(request)] = row
        self._started[id(request)] = time.perf_counter()

    async def response(self, response: httpx.Response) -> None:
        row = self._requests.pop(id(response.request))
        row["latency_seconds"] = time.perf_counter() - self._started.pop(id(response.request))
        row["status_code"] = response.status_code
        await response.aread()
        try:
            payload = response.json()
        except ValueError:
            row["response_shape"] = "non_json"
            return
        if not isinstance(payload, dict):
            row["response_shape"] = "non_object"
            return
        if self.protocol is ModelProtocol.GEMINI:
            usage = GeminiProvider._usage_diagnostics(payload)["usage"]
        elif self.protocol is ModelProtocol.RESPONSES:
            usage = DeepSeekResponsesProvider._reported_usage(response)
        else:
            raw = payload.get("usage")
            raw = raw if isinstance(raw, dict) else {}
            details = raw.get("prompt_tokens_details")
            details = details if isinstance(details, dict) else {}
            usage = {
                "prompt_tokens": raw.get("prompt_tokens"),
                "completion_tokens": raw.get("completion_tokens"),
                "total_tokens": raw.get("total_tokens"),
                "cached_prompt_tokens": details.get(
                    "cached_tokens", raw.get("prompt_cache_hit_tokens")
                ),
            }
            usage = {
                key: value for key, value in usage.items() if type(value) is int and value >= 0
            }
        row.update(usage)
        row["unknown_usage"] = type(row.get("total_tokens")) is not int
        row["response_shape"] = self.protocol.value + "_object"
        error = payload.get("error")
        if isinstance(error, dict):
            error_categories = {
                "invalid_request_error",
                "invalid_request",
                "authentication_error",
                "rate_limit_error",
                "server_error",
                "invalid_parameter_error",
                "not_found_error",
                "api_error",
                "invalid_api_key",
                "quota_exceeded",
                "invalid_json_schema",
                "unsupported_response_format",
            }
            row["provider_error_category"] = {
                key: value if value in error_categories else "other"
                for key in ("type", "code")
                if isinstance(value := error.get(key), str)
            }
        # Do not emit error text, candidates, signatures, body, or headers.

    def finish_logical_call(self, error_category: str | None) -> None:
        for request_id, row in self._requests.items():
            row["error_category"] = error_category or "no_response"
            row["latency_seconds"] = time.perf_counter() - self._started[request_id]
        self._requests.clear()
        self._started.clear()


async def isolated_application(root: Path) -> tuple[ApplicationContainer, tuple[Any, ...]]:
    # model_validate does not read .env or inherit BaseSettings environment values.
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite+aiosqlite:///{(root / 'probe.sqlite3').as_posix()}",
            "llm_provider": "fake",
            "llm_model": "fake",
            "model_profiles_file": root / "absent.toml",
            "model_profiles_legacy_compatibility": True,
            "workspace_directory": root / "workspace",
            "conversation_media_cache_directory": root / "media",
            "social_transfer_directory": root / "social-transfer",
            "plugin_directory": root / "plugins",
            "plugin_system_enabled": False,
            "mcp_config_path": root / "absent-mcp.json",
            "mcp_enabled": False,
            "sandbox_socket": root / "absent-sandbox.sock",
            "web_mode": "disabled",
            "web_search_bridge_state_path": root / "search.sqlite3",
            "emoji_storage_root": root / "emoji",
        }
    )
    database = Database(settings.database_url)
    await database.create_schema()
    app = ApplicationContainer(settings, database=database)
    # No start(), gateway connection, plugin activation, or Work scheduler.
    return app, await app.main_agent_contract.definitions()


def profile_catalog(profile: ModelProfile) -> ModelProfileCatalog:
    return ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
    )


def synthetic_prefix(namespace: str, characters: int) -> tuple[ChatMessage, ...]:
    line = "研究群合成历史：讨论文档格式、颜色、排序；本段不含真实人物或私人信息。\n"
    history = (line * (characters // len(line) + 1))[:characters]
    return (
        ChatMessage("system", "合成缓存实验：只读合成资料、保存合成线索；简短回答。"),
        ChatMessage("user", f"实验命名空间 {namespace}\n{history}"),
    )


async def run_experiment(
    *,
    profile: ModelProfile,
    api_key: str,
    output: Path,
    scenarios: tuple[str, ...] = ("C01",),
    samples: int = 3,
    warmups: int = 1,
    prefix_characters: int = 12000,
    delay_seconds: float = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    manifest: tuple[Any, ...] | None = None,
    prefix_control: bool = True,
    compaction_mode: str = "ordinary",
) -> dict[str, Any]:
    if not (
        (profile.protocol is ModelProtocol.GEMINI and profile.provider == "gemini")
        or (
            profile.provider == "deepseek"
            and profile.protocol in {ModelProtocol.CHAT_COMPLETIONS, ModelProtocol.RESPONSES}
        )
    ):
        raise ValueError("manual experiment requires a Gemini or DeepSeek profile")
    if compaction_mode not in {"ordinary", "work"}:
        raise ValueError("invalid manual compaction mode")
    if samples < 1 or warmups < 1 or prefix_characters < 1 or delay_seconds < 0:
        raise ValueError("invalid manual experiment parameters")
    if (
        not scenarios
        or len(set(scenarios)) != len(scenarios)
        or not set(scenarios) <= set(SCENARIOS)
    ):
        raise ValueError("invalid manual scenarios")
    namespace = uuid4().hex
    report: dict[str, Any] = {
        "schema_version": 1,
        "namespace": namespace,
        "observed_at": datetime.now(UTC).isoformat(),
        "provider": profile.provider,
        "protocol": profile.protocol.value,
        "profile_id": profile.id,
        "model": profile.model,
        "endpoint_origin": endpoint_origin(profile.base_url),
        "transport": "mock" if transport is not None else "real_api",
        "scope": "synthetic serializer experiment; no QQ or production database",
        "compaction_mode": compaction_mode,
        "parameters": {
            "scenarios": list(scenarios),
            "warmups": warmups,
            "measured_samples": samples,
            "prefix_characters": prefix_characters,
            "delay_seconds": delay_seconds,
            "prefix_control": prefix_control,
        },
        "upstream_wire_verified": False,
        "profile_settings": {
            "default_max_output_tokens": profile.default_max_output_tokens,
            "max_input_tokens": profile.max_input_tokens,
            "context_window_tokens": profile.context_window_tokens,
            "max_output_tokens_limit": profile.max_output_tokens_limit,
            "temperature": profile.default_temperature,
            "reasoning_effort": profile.reasoning_effort.value,
            "structured_output_mode": profile.structured_output_mode.value,
            "timeout_seconds": profile.timeout_seconds,
            "max_retries": profile.max_retries,
        },
        "scenarios": [],
        "logical_calls": [],
        "physical_calls": [],
    }
    physical = PhysicalRequests(profile.protocol)
    with tempfile.TemporaryDirectory(prefix="yuki-cache-probe-") as temporary:
        root = Path(temporary)
        app, tools = await isolated_application(root)
        if manifest is not None:
            tools = manifest
        report["manifest"] = {
            "tools": len(tools),
            "origin": "explicit frozen manifest"
            if manifest is not None
            else "isolated MainAgentContract",
            "sha256": digest(
                [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in tools
                ]
            ),
            "external_plugins_loaded": False,
        }
        client = httpx.AsyncClient(
            base_url=profile.base_url.rstrip("/") + "/",
            transport=transport,
            event_hooks={"request": [physical.request], "response": [physical.response]},
        )
        provider_class = (
            GeminiProvider
            if profile.protocol is ModelProtocol.GEMINI
            else DeepSeekResponsesProvider
            if profile.protocol is ModelProtocol.RESPONSES
            else OpenAICompatibleProvider
        )
        provider_options: dict[str, Any] = {}
        if profile.protocol is ModelProtocol.CHAT_COMPLETIONS:
            provider_options["provider_name"] = "deepseek"
        if profile.protocol is not ModelProtocol.RESPONSES:
            provider_options["options"] = profile.wire_options
        report["provider_class"] = provider_class.__name__
        report["provider_session_headers"] = sorted(profile.headers)
        provider = provider_class(
            base_url=profile.base_url,
            api_key=api_key,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
            client=client,
            headers=profile.headers,
            **provider_options,
        )
        executor = TaskModelExecutor(
            router=ModelRouter(profile_catalog(profile)),
            pool=ModelClientPool(injected_profiles={profile.id: provider}),
            invocations=ModelInvocationRepository(app.database),
        )
        original_execute = executor.execute

        async def tracked_execute(task: Any, request: ChatRequest, **options: Any) -> Any:
            if physical.sample.get("stage") != "auxiliary_compaction":
                return await original_execute(task, request, **options)
            logical = {
                **physical.sample,
                "physical_start": len(physical.rows) + 1,
                "request_chain_id": request.request_chain_id,
            }
            report["logical_calls"].append(logical)
            try:
                response = await original_execute(task, request, **options)
                logical.update(success=True, response_status=response.status.value)
                logical["summary_shape"] = summary_shape(
                    response.content,
                    ordinary=compaction_mode == "ordinary",
                )
                if compaction_mode == "work":
                    logical["summary_reference_shape"] = summary_reference_shape(
                        response.content, json.loads(request.messages[-1].content)
                    )
                physical.finish_logical_call(None)
                return response
            except Exception as error:
                logical.update(success=False, error_category=type(error).__name__)
                physical.finish_logical_call(type(error).__name__)
                raise
            finally:
                logical["physical_end"] = len(physical.rows)

        executor.execute = tracked_execute
        store = ToolArtifactRepository(app.database, root / "raw-results", retention_seconds=86400)
        try:
            for scenario in scenarios:
                if scenario not in SCENARIOS:
                    raise ValueError("unknown scenario")
                entry: dict[str, Any] = {"scenario": scenario, "status": "running"}
                report["scenarios"].append(entry)
                if scenario == "C08":
                    entry["history_mode"] = (
                        "one cumulative transcript: assistant reply then next user"
                    )
                    entry["provider_session_header_injected"] = False
                prefix = synthetic_prefix(namespace + "-" + scenario, prefix_characters)
                transcript = TurnTranscript(prefix)
                driver = (
                    WorkDriver(app, root, namespace, scenario, prefix)
                    if scenario in {"C03", "C05"}
                    or (scenario == "C06" and compaction_mode == "work")
                    else None
                )
                if scenario == "C06" and compaction_mode == "ordinary":
                    driver = OrdinaryDriver(app, prefix)
                if driver is not None:
                    await driver.initialize()
                    entry["runtime_evidence"] = driver.evidence
                handle = None
                if scenario in {"C04", "C05"}:
                    handle = await store.write_artifact(
                        provider_id="core",
                        tool_name="read",
                        content="合成原件正文\n" * 8000,
                        media_type="text/plain",
                    )
                    transcript.append(
                        ChatMessage(
                            "user", f"资料目录：{handle}，需要时调用 read_tool_artifact 读取。"
                        )
                    )
                if scenario == "C07":
                    entry["mcp_status"] = "not_applicable: no external MCP server is contacted"
                    if not any(
                        capability.value == "image_input" for capability in profile.capabilities
                    ):
                        entry.update(
                            status="not_applicable",
                            reason="selected profile does not declare vision",
                        )
                        continue
                    transcript.append(
                        ChatMessage(
                            "user",
                            "合成一像素图片",
                            images=(ChatImage(data_url="data:image/png;base64," + PNG),),
                        )
                    )
                for index in range(warmups + samples):
                    stage = "warmup" if index < warmups else "measured"
                    if scenario in {"C02", "C04", "C05", "C06"} and index == 0:
                        text = (
                            f"仅调用 read_tool_artifact，handle={handle}，"
                            "operation=text，limit=100。"
                            if handle
                            else "仅调用 get_recent_chat_history，读取当前合成会话历史。"
                        )
                    else:
                        text = f"实验问题 {index}：请简短回答文档颜色建议，不执行其他操作。"
                    if scenario == "C08":
                        text = (
                            f"同一会话第 {index + 1} 轮：继续合成配色讨论，"
                            "给出两句话的新建议；不调用工具。"
                        )
                    if scenario == "C03" and index == 0:
                        text = (
                            "仅调用 task_control，action=update，context_note="
                            '{"version":1,"unresolved":[{"text":"合成配色比较尚待核实",'
                            '"refs":["goal"]}]}。不要修改目标或发送消息。'
                        )
                    if scenario == "C01":
                        current = TurnTranscript(prefix)
                        current.append(ChatMessage("user", text))
                    elif driver is not None:
                        current = await driver.prepare(index)
                        current.append(ChatMessage("user", text))
                    else:
                        current = transcript
                        current.append(ChatMessage("user", text))
                    sequence = current.request()
                    request = ChatRequest(
                        messages=sequence.messages,
                        tools=tools,
                        continuation=sequence.continuation,
                        continuation_items=sequence.items,
                        request_chain_id=current.chain_id,
                    )
                    physical.sample = {"scenario": scenario, "stage": stage, "sample_index": index}
                    logical = {
                        **physical.sample,
                        "physical_start": len(physical.rows) + 1,
                        "request_chain_id": request.request_chain_id,
                    }
                    report["logical_calls"].append(logical)
                    try:
                        response = await executor.execute(ModelTask.CHAT_AGENT, request)
                    except Exception as error:
                        logical.update(success=False, error_category=type(error).__name__)
                        physical.finish_logical_call(type(error).__name__)
                        logical["physical_end"] = len(physical.rows)
                        entry["status"] = "failed"
                        if driver is not None:
                            await driver.close()
                        break
                    logical.update(success=True, response_status=response.status.value)
                    physical.finish_logical_call(None)
                    logical["physical_end"] = len(physical.rows)
                    if scenario != "C01":
                        if isinstance(driver, OrdinaryDriver):
                            driver.observe_response(response)
                        if response.continuation is not None:
                            current.accept(response.continuation)
                        else:
                            current.append(
                                ChatMessage(
                                    "assistant", response.content, tool_calls=response.tool_calls
                                )
                            )
                        for call in response.tool_calls:
                            if (
                                driver is not None
                                and driver.control is not None
                                and call.function.name == "task_control"
                            ):
                                arguments = json.loads(call.function.arguments)
                                if arguments.get("action") == "update" and set(arguments) <= {
                                    "action",
                                    "context_note",
                                }:
                                    result = json.loads(
                                        await driver.control.execute(
                                            call.function.name,
                                            arguments,
                                            driver.control.session.call_key(call.id),
                                        )
                                    )
                                    entry["context_note_result_ok"] = result.get("ok", False)
                                else:
                                    result = {
                                        "ok": False,
                                        "executed": False,
                                        "error": "synthetic_backend_note_only",
                                    }
                            elif call.function.name == "read_tool_artifact" and handle:
                                arguments = json.loads(call.function.arguments)
                                if arguments.get("handle") == handle:
                                    result = await store.read(
                                        handle,
                                        operation="text",
                                        offset=int(arguments.get("offset", 0)),
                                        limit=min(int(arguments.get("limit", 100)), 8000),
                                    )
                                    entry["local_raw_read"] = result is not None
                                else:
                                    result = {"ok": False, "error": "unknown_synthetic_handle"}
                            elif call.function.name == "get_recent_chat_history":
                                result = {
                                    "ok": True,
                                    "data": {
                                        "origin": "synthetic_fixture",
                                        "messages": [message.content for message in prefix[1:]],
                                    },
                                }
                                entry["synthetic_history_read"] = True
                            else:
                                result = {
                                    "ok": False,
                                    "executed": False,
                                    "error": "synthetic_backend_no_external_effects",
                                }
                            recorded_result = (
                                await driver.record_read(call, result)
                                if scenario == "C05" and entry.get("local_raw_read")
                                else json.dumps(result, ensure_ascii=False)
                            )
                            current.append_result(call.id, recorded_result)
                            if isinstance(driver, OrdinaryDriver):
                                driver.observe_result(call, recorded_result)
                        if scenario == "C04" and index == 1 and entry.get("local_raw_read"):
                            # Explicit new request: retain public prefix + source reference.
                            transcript = TurnTranscript(
                                (
                                    *prefix,
                                    ChatMessage(
                                        "user",
                                        f"原件引用 {handle}；读取状态以原工具结果为准。"
                                        "正文退出当前工作区。",
                                    ),
                                )
                            )
                            logical["boundary_after"] = "explicit_body_exit"
                    if driver is not None:
                        await driver.save(current)
                        if index == 0 and scenario == "C05":
                            await driver.reopen()
                        elif index == 0 and scenario == "C06":
                            physical.sample = {
                                "scenario": scenario,
                                "stage": "auxiliary_compaction",
                                "sample_index": index,
                            }
                            start = len(physical.rows)
                            try:
                                await driver.compact(executor, request)
                            except Exception as error:
                                entry.update(
                                    status="compaction_failed", error_category=type(error).__name__
                                )
                                if re.fullmatch(r"(?:work|ordinary)_[a-z_]+", str(error)):
                                    entry["error_code"] = str(error)
                                physical.finish_logical_call(type(error).__name__)
                                await driver.close()
                                break
                            physical.finish_logical_call(None)
                            entry["auxiliary_physical_requests"] = len(physical.rows) - start
                    if delay_seconds:
                        await asyncio.sleep(delay_seconds)
                if entry["status"] == "running":
                    entry["status"] = "complete"
                    if scenario == "C02" and not entry.get("synthetic_history_read"):
                        entry.update(status="model_did_not_select_tool")
                    elif scenario == "C04" and not entry.get("local_raw_read"):
                        entry.update(status="model_did_not_read_raw_result")
                    elif scenario == "C05" and not entry.get("local_raw_read"):
                        entry.update(status="model_did_not_read_before_restart")
                    elif scenario == "C03" and not driver.evidence.get("selected_observation_ids"):
                        entry.update(status="model_did_not_publish_context_note")
                if driver is not None:
                    await driver.close()
                if scenario == "C01" and prefix_control and entry["status"] == "complete":
                    # This is a nonshared-prefix control, not a claim that cache is disabled.
                    control_namespace = uuid4().hex + "-control"
                    entry["control_namespace"] = control_namespace
                    control_prefix = synthetic_prefix(control_namespace, prefix_characters)
                    control_request = ChatRequest(
                        messages=(
                            *control_prefix,
                            ChatMessage("user", "独立对照：简短回答颜色建议。"),
                        ),
                        tools=tools,
                    )
                    physical.sample = {
                        "scenario": scenario,
                        "stage": "prefix_control",
                        "sample_index": 0,
                    }
                    logical = {
                        **physical.sample,
                        "physical_start": len(physical.rows) + 1,
                        "request_chain_id": control_request.request_chain_id,
                    }
                    report["logical_calls"].append(logical)
                    try:
                        response = await executor.execute(ModelTask.CHAT_AGENT, control_request)
                        logical.update(success=True, response_status=response.status.value)
                        physical.finish_logical_call(None)
                    except Exception as error:
                        logical.update(success=False, error_category=type(error).__name__)
                        physical.finish_logical_call(type(error).__name__)
                    logical["physical_end"] = len(physical.rows)
            report["physical_calls"] = physical.rows
            report["totals"] = weighted_usage(physical.rows)
            report["successful_http_totals"] = weighted_usage(
                [row for row in physical.rows if row.get("status_code") == 200]
            )
            report["unknown_usage_requests"] = sum(row["unknown_usage"] for row in physical.rows)
            async with app.database.sessions() as reader:
                recorded = (
                    await reader.scalars(
                        select(ModelInvocationModel).order_by(ModelInvocationModel.id)
                    )
                ).all()
            report["recorded_invocations"] = [
                {
                    field: getattr(row, field)
                    for field in (
                        "id",
                        "task",
                        "profile_id",
                        "provider",
                        "model",
                        "success",
                        "prompt_tokens",
                        "cached_prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "physical_request_count",
                        "unknown_usage_request_count",
                        "error_category",
                    )
                }
                for row in recorded
            ]
            report["accounting"] = {
                "recorded_logical_invocations": len(recorded),
                "recorded_physical_requests": sum(
                    row.physical_request_count or 0 for row in recorded
                ),
                "observed_physical_requests": len(physical.rows),
                "recorded_unknown_usage_requests": sum(
                    row.unknown_usage_request_count or 0 for row in recorded
                ),
                "observed_unknown_usage_requests": report["unknown_usage_requests"],
                "observed_logical_calls": len(report["logical_calls"]),
            }
            report["accounting"]["physical_match"] = report["accounting"][
                "recorded_physical_requests"
            ] == len(physical.rows)
            report["accounting"]["unknown_usage_match"] = (
                report["accounting"]["recorded_unknown_usage_requests"]
                == report["unknown_usage_requests"]
            )
            report["accounting"]["logical_match"] = len(report["logical_calls"]) == len(recorded)
            for entry in report["scenarios"]:
                rows = [row for row in physical.rows if row["scenario"] == entry["scenario"]]
                entry["cold"] = weighted_usage(
                    [row for row in rows if row["stage"] == "warmup" and row["sample_index"] == 0]
                )
                entry["preheat"] = weighted_usage([row for row in rows if row["stage"] == "warmup"])
                entry["hot"] = weighted_usage([row for row in rows if row["stage"] == "measured"])
                entry["auxiliary"] = weighted_usage(
                    [row for row in rows if row["stage"] == "auxiliary_compaction"]
                )
                entry["prefix_control"] = weighted_usage(
                    [row for row in rows if row["stage"] == "prefix_control"]
                )
        except Exception as error:
            # Preserve all transport evidence even if fixture preparation or a
            # malformed tool call aborts the remaining scenarios.
            report["experiment_error_category"] = type(error).__name__
        finally:
            report["physical_calls"] = physical.rows
            report["totals"] = weighted_usage(physical.rows)
            report["unknown_usage_requests"] = sum(row["unknown_usage"] for row in physical.rows)
            await executor.close()
            await client.aclose()
            # Constructors registered resources but lifecycle was never started.
            # Only close callbacks run; no scheduled work or gateway is started.
            errors = await app.lifecycle._close_entries(app.lifecycle._entries)
            if errors:
                report["cleanup_errors"] = [type(error).__name__ for error in errors]
            await app.database.close()
    report["finished_at"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        output.write_text, json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly authorize API transport")
    routes = parser.add_mutually_exclusive_group(required=True)
    routes.add_argument("--profiles", type=Path)
    routes.add_argument("--route-stdin", action="store_true", help="private JSON profile/key pipe")
    parser.add_argument("--profile")
    parser.add_argument("--provider", choices=("gemini", "deepseek"), default="gemini")
    parser.add_argument(
        "--protocol",
        choices=("gemini", "chat_completions", "responses"),
        help="explicit experiment protocol override; does not edit saved profiles",
    )
    parser.add_argument(
        "--base-url", help="explicit transport tunnel override; does not change model"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="read-only frozen deployment tools export")
    parser.add_argument("--scenarios", default="C01")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--prefix-characters", type=int, default=12000)
    parser.add_argument("--delay-seconds", type=float, default=0)
    parser.add_argument("--compaction-mode", choices=("ordinary", "work"), default="ordinary")
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "no API request is made without --live; use offline unit tests for dry validation"
        )
    logging.getLogger().setLevel(logging.CRITICAL)
    if args.route_stdin:
        profile, api_key = route_from_stdin(sys.stdin)
    else:
        if not args.profile:
            parser.error("--profiles requires --profile")
        environment = dict(os.environ)
        _, secrets = read_model_secrets(args.profiles)
        environment.update(secrets)
        catalog = parse_model_profile_catalog(
            args.profiles.read_text(encoding="utf-8"), environment=environment
        )
        profile = catalog.profiles[args.profile]
        api_key = environment.get(profile.api_key_env, "")
    if not api_key:
        parser.error("selected profile credential is unavailable")
    if profile.provider != args.provider:
        parser.error("selected profile does not match --provider")
    original_origin = endpoint_origin(profile.base_url)
    original_protocol = profile.protocol.value
    if args.protocol:
        data = profile.model_dump(mode="json")
        data["protocol"] = args.protocol
        if args.protocol == "responses":
            data["wire_options"] = None
        profile = ModelProfile.model_validate(data)
    if args.base_url:
        profile = profile.model_copy(update={"base_url": args.base_url})
    report = asyncio.run(
        run_experiment(
            profile=profile,
            api_key=api_key,
            output=args.output,
            scenarios=tuple(args.scenarios.split(",")),
            samples=args.samples,
            warmups=args.warmups,
            prefix_characters=args.prefix_characters,
            delay_seconds=args.delay_seconds,
            manifest=load_manifest(args.manifest) if args.manifest else None,
            compaction_mode=args.compaction_mode,
        )
    )
    report["route"] = {
        "configured_origin": original_origin,
        "transport_origin": endpoint_origin(profile.base_url),
        "explicit_tunnel_override": bool(args.base_url),
        "configured_protocol": original_protocol,
        "experiment_protocol": profile.protocol.value,
        "explicit_protocol_override": bool(args.protocol),
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.output), "totals": report["totals"]}, ensure_ascii=False))
    if report.get("experiment_error_category"):
        raise SystemExit("manual experiment aborted; transport evidence is retained in report")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Provider/config exceptions can contain private raw inputs. Emit type only.
        raise SystemExit(f"manual cache experiment failed: {type(error).__name__}") from None
