"""Manual A/B/C Main replay; default transport is offline and never starts a Bot.

A imports an explicitly supplied 24da979 checkout. B and C import a frozen copy
of the candidate; B changes only its in-process material comparison to emit the
remaining current material in full. No product configuration or CI gate is added.
Live input replay requires --live and a private profile/key pipe on stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def profile_data(profile, *, private_headers: bool = False) -> dict[str, Any]:
    value = profile.model_dump(mode="json", exclude=set() if private_headers else {"headers"})
    if profile.wire_options is not None:
        value["wire_options"] = profile.wire_options.model_dump(mode="json", exclude_unset=True)
    return value


def closed_loop_system(original: str) -> str:
    return original.replace(
        "Read the synthetic request; return a brief internal answer without tools.",
        "Complete the synthetic read-only conversation task. Call get_my_capabilities "
        "with mode=summary once, then give a brief internal answer about the current clue "
        "and original note evidence. The next synthetic user may quote your actual previous "
        "internal answer. Do not send a QQ message, change state, or create other work.",
    )


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def probe_helpers(path: Path):
    """Shared hook code imports this child process's selected product source."""
    spec = importlib.util.spec_from_file_location("_materials_probe_hooks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def freeze_source(source: Path, target: Path) -> dict[str, Any]:
    """Only versioned/unignored code and fixtures; never .env, data or credentials."""
    tracked = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--",
            "src",
            "tests",
            "tools",
            "migrations",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=source,
        text=True,
    ).splitlines()
    added = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "src",
            "tests",
            "tools",
            "migrations",
        ],
        cwd=source,
        text=True,
    ).splitlines()
    fingerprints = {}
    for name in sorted(set(tracked + added)):
        original = source / name
        if not original.is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
        fingerprints[name] = sha(destination.read_bytes())
    return {
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip(),
        "dirty": subprocess.check_output(
            ["git", "status", "--porcelain", "--", "src", "tests", "tools", "migrations"],
            cwd=source,
            text=True,
        ).splitlines(),
        "files": fingerprints,
        "files_sha256": sha(json.dumps(fingerprints, sort_keys=True).encode()),
    }


def verify_baseline(source: Path, repository: Path) -> dict[str, Any]:
    commit = "24da979dd32b4c38922b48e682effddf3955e027"
    listing = subprocess.check_output(
        [
            "git",
            "ls-tree",
            "-r",
            commit,
            "--",
            "src",
            "tests",
            "tools",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=repository,
        text=True,
    )
    fingerprints = {}
    normalized_files = []
    for row in listing.splitlines():
        metadata, name = row.split("\t", 1)
        original = (source / name).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(original)).encode() + b"\0" + original).hexdigest()
        if actual != metadata.split()[2]:
            canonical = original.replace(b"\r\n", b"\n")
            normalized = hashlib.sha1(
                b"blob " + str(len(canonical)).encode() + b"\0" + canonical
            ).hexdigest()
            if normalized != metadata.split()[2]:
                raise ValueError(f"baseline file differs from 24da979: {name}")
            normalized_files.append(name)
        fingerprints[name] = sha(original)
    return {
        "commit": commit,
        "tracked_files_verified": len(fingerprints),
        "crlf_normalized_for_git_blob_verification": normalized_files,
        "files_sha256": sha(json.dumps(fingerprints, sort_keys=True).encode()),
    }


def usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("prompt_tokens", "cached_prompt_tokens", "completion_tokens", "total_tokens")
    totals = {}
    for field in fields:
        known = [row[field] for row in rows if type(row.get(field)) is int and row[field] >= 0]
        subtotal = sum(known) if known else None
        totals[field] = {
            "known_requests": len(known),
            "known_subtotal": subtotal,
            "complete_total": subtotal if len(known) == len(rows) else None,
        }
    eligible = [
        row
        for row in rows
        if type(row.get("prompt_tokens")) is int
        and type(row.get("cached_prompt_tokens")) is int
        and 0 <= row["cached_prompt_tokens"] <= row["prompt_tokens"]
    ]
    incoming = sum(row["prompt_tokens"] for row in eligible)
    cached = sum(row["cached_prompt_tokens"] for row in eligible)
    return {
        "physical_requests": len(rows),
        "fields": totals,
        "cache_eligible_requests": len(eligible),
        "cache_eligible_input": incoming if eligible else None,
        "cache_eligible_cached": cached if eligible else None,
        "weighted_cache_ratio": cached / incoming if incoming else None,
        "explicit_zero_cached_requests": sum(row["cached_prompt_tokens"] == 0 for row in eligible),
        "cache_unknown_or_invalid_requests": len(rows) - len(eligible),
        "complete_cost": None,
        "api_equivalent_cost": None,
        "actual_bill": None,
        "quota_unit": None,
    }


def wire_texts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Material text only; native tool/results and schemas remain in wire bytes."""
    if "contents" in payload:
        return (
            [
                part["text"]
                for message in payload["contents"]
                for part in message["parts"]
                if "text" in part
            ],
            [
                part["text"]
                for part in payload.get("systemInstruction", {}).get("parts", [])
                if "text" in part
            ],
        )
    texts, system = [], []
    for message in payload.get("messages", payload.get("input", [])):
        if message.get("role") == "tool":
            continue
        content = message.get("content", [])
        values = (
            [content]
            if isinstance(content, str)
            else [part["text"] for part in content if "text" in part]
            if isinstance(content, list)
            else []
        )
        (system if message.get("role") in {"system", "developer"} else texts).extend(values)
    if "instructions" in payload:
        system.append(payload["instructions"])
    return texts, system


def response_metadata(payload: dict[str, Any], protocol: str, *, real: bool) -> dict[str, Any]:
    result = {
        "metering_kind": "real_numeric_response" if real else "mock_no_real_usage",
    }
    if protocol == "gemini":
        result.update(
            candidate_finish_reasons=[
                item.get("finishReason") for item in payload.get("candidates", [])
            ],
            public_model_text=[
                part["text"]
                for candidate in payload.get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
                if "text" in part and not part.get("thought")
            ],
            model_tool_calls=[
                part["functionCall"]["name"]
                for candidate in payload.get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
                if "functionCall" in part
            ],
        )
    elif protocol == "chat_completions":
        result.update(
            candidate_finish_reasons=[
                item.get("finish_reason") for item in payload.get("choices", [])
            ],
            public_model_text=[
                item["message"]["content"]
                for item in payload.get("choices", [])
                if isinstance(item.get("message", {}).get("content"), str)
            ],
            model_tool_calls=[
                call["function"]["name"]
                for item in payload.get("choices", [])
                for call in item.get("message", {}).get("tool_calls", [])
            ],
        )
    else:
        result.update(
            candidate_finish_reasons=[payload.get("status")],
            public_model_text=[
                part["text"]
                for item in payload.get("output", [])
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ],
            model_tool_calls=[
                item["name"]
                for item in payload.get("output", [])
                if item.get("type") == "function_call"
            ],
        )
    return result


def captured_generation(root: Path) -> dict[str, Any]:
    from tools.cache_probe import generation_shape

    return {
        arm: [
            {
                "sample_index": index,
                "body_sha256": sha(body.encode()),
                "generation": generation_shape(json.loads(body)),
            }
            for index, body in enumerate(
                (root / arm / "bodies.jsonl").read_text(encoding="utf-8").splitlines()
            )
        ]
        for arm in "ABC"
    }


def measure_bodies(root: Path) -> dict[str, Any]:
    """Structural attribution for these synthetic fixtures, never a product filter."""
    from tools.cache_probe import input_units, prefix_comparison

    header = "本轮运行资料（按 trust 字段区分可信度）："
    note_header = "[Observation; derived notes]\n"

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    common = json.loads((root / "seed" / "common.json").read_text(encoding="utf-8"))
    fixture = common["retired_fixture_sources"]
    results, bodies_by_arm = {}, {}
    for arm in "ABC":
        bodies = (root / arm / "bodies.jsonl").read_text(encoding="utf-8").splitlines()
        physical = json.loads((root / arm / "physical.json").read_text(encoding="utf-8"))
        # These sources already had a selected model expression before replay.
        seen = {(common["view_key"], fixture[name]) for name in ("auto", "mixed", "old_uncovered")}
        measured = []
        previous_body = None
        for body, row in zip(bodies, physical, strict=True):
            payload = json.loads(body)
            texts, system_texts = wire_texts(payload)
            metrics = {
                "physical_index": row["physical_index"],
                "scenario": row["scenario"],
                "wire_body_bytes": len(body.encode()),
                "contents_roles": len(
                    payload.get("contents", payload.get("messages", payload.get("input", [])))
                ),
                "parts": len(input_units(payload)),
                "compiled_envelopes": 0,
                "derived_notes": 0,
                "retired_snapshot_packets": 0,
                "retired_auto_summary_packets": 0,
            }
            for name in (
                "model_text",
                "compiled_text",
                "derived_text",
                "all_junk_payload",
                "new_junk_payload",
                "necessary_baseline_payload",
                "necessary_change_payload",
                "necessary_note_payload",
                "runtime_wrapping",
                "chat_text",
                "other_control",
            ):
                metrics[name + "_chars"] = metrics[name + "_bytes"] = 0

            def add(name, text_value, destination=metrics):
                destination[name + "_chars"] += len(text_value)
                destination[name + "_bytes"] += len(text_value.encode())

            for text_value in system_texts + texts:
                add("model_text", text_value)
            provenance = {
                item["message"].get("content"): item.get("observation_id")
                for item in row["projection"]["selected_fragments"]
            }
            visible = {}
            for text_value in texts:
                if text_value.startswith(note_header):
                    metrics["derived_notes"] += 1
                    add("derived_text", text_value)
                    raw = text_value[len(note_header) :]
                    source = provenance.get(text_value)
                    junk = source in {fixture["old"], fixture["old_uncovered"], fixture["auto"]}
                    if junk:
                        add("all_junk_payload", raw)
                        identity = (row["projection"]["view_key"], source)
                        if identity not in seen:
                            add("new_junk_payload", raw)
                        seen.add(identity)
                        metrics[
                            "retired_auto_summary_packets"
                            if source == fixture["auto"]
                            else "retired_snapshot_packets"
                        ] += 1
                    else:
                        add("necessary_note_payload", raw)
                    add("runtime_wrapping", note_header)
                    continue
                if not text_value.startswith(header):
                    add(
                        "chat_text" if re.search(r"#\d+>", text_value) else "other_control",
                        text_value,
                    )
                    continue
                raw, chat_text = text_value.split("\n\n", 1)
                contributions = json.loads(raw[len(header) :])
                metrics["compiled_envelopes"] += 1
                add("compiled_text", raw)
                add("chat_text", chat_text)
                all_data = []
                is_new = f"SYNTHETIC_EVENT_{row['step']:02}:" in chat_text
                for contribution in contributions:
                    identity, data = contribution["id"], contribution["data"]
                    original = encode(data)
                    all_data.append(original)
                    if identity == "runtime.time":
                        junk_text = original
                    elif identity in {"runtime.short_state", "context.people_and_scene"}:
                        if identity == "runtime.short_state":
                            effective = [
                                {"slot": item["slot"], "text": item["text"]}
                                for item in data
                                if "text" in item
                            ]
                            unit_key, units = "slot", effective
                        else:
                            units = [
                                item
                                for item in data["items"]
                                if item["id"] != "event_bound_memory_refs"
                            ]
                            effective, unit_key = {"items": units}, "id"
                        before = visible.setdefault(identity, {})
                        first = not before
                        changes = [item for item in units if before.get(item[unit_key]) != item]
                        delta = (
                            encode(
                                changes if identity == "runtime.short_state" else {"items": changes}
                            )
                            if changes
                            else ""
                        )
                        # Count retained payload bytes exactly; syntax belonging to
                        # removed fields/unchanged items is the structural remainder.
                        necessary = delta if changes else ""
                        add(
                            "necessary_baseline_payload" if first else "necessary_change_payload",
                            necessary,
                        )
                        for item in units:
                            before[item[unit_key]] = item
                        junk_chars = len(original) - len(necessary)
                        junk_bytes = len(original.encode()) - len(necessary.encode())
                        metrics["all_junk_payload_chars"] += junk_chars
                        metrics["all_junk_payload_bytes"] += junk_bytes
                        if is_new:
                            metrics["new_junk_payload_chars"] += junk_chars
                            metrics["new_junk_payload_bytes"] += junk_bytes
                        continue
                    else:
                        # This fixture does not exercise every plugin/tool producer.
                        add("necessary_baseline_payload", original)
                        junk_text = ""
                    add("all_junk_payload", junk_text)
                    if is_new:
                        add("new_junk_payload", junk_text)
                metrics["runtime_wrapping_chars"] += len(raw) - sum(map(len, all_data))
                metrics["runtime_wrapping_bytes"] += len(raw.encode()) - sum(
                    len(data.encode()) for data in all_data
                )
            current_bytes = body.encode()
            metrics["raw_body_lcp_bytes"] = (
                next(
                    (
                        i
                        for i, (left, right) in enumerate(
                            zip(previous_body, current_bytes, strict=False)
                        )
                        if left != right
                    ),
                    min(len(previous_body), len(current_bytes)),
                )
                if previous_body is not None
                else None
            )
            previous_body = current_bytes
            measured.append(metrics)
        columns = [
            key
            for key in measured[0]
            if key.endswith(("_chars", "_bytes")) and key != "raw_body_lcp_bytes"
        ] + [
            "compiled_envelopes",
            "derived_notes",
            "retired_snapshot_packets",
            "retired_auto_summary_packets",
        ]
        results[arm] = {
            "totals": {key: sum(row[key] for row in measured) for key in columns},
            "per_request": measured,
        }
        bodies_by_arm[arm] = bodies
    paired = {}
    for old, new in ("AB", "BC", "AC"):
        old_totals, new_totals = results[old]["totals"], results[new]["totals"]
        paired[old + "_to_" + new] = {
            key: {
                "old": value,
                "new": new_totals[key],
                "reduction": value - new_totals[key],
                "reduction_ratio": (value - new_totals[key]) / value if value else None,
            }
            for key, value in old_totals.items()
        }
        paired[old + "_to_" + new]["request_comparisons"] = [
            prefix_comparison(json.loads(left), json.loads(right))
            for left, right in zip(bodies_by_arm[old], bodies_by_arm[new], strict=True)
        ]
    result = {
        "scope": (
            "synthetic structural payload attribution; model text excludes tool schemas; "
            "retained original baselines reread from history are useful, not duplicate junk; "
            "actual private/Work control is other_control"
        ),
        "unknown": [
            "exact token attribution",
            "real cache",
            "cost and bill",
            "tool/native/private/artifact scenarios beyond this fixture",
        ],
        "arms": results,
        "paired": paired,
    }
    write_json(root / "wire-metrics.json", result)
    return result


def scenarios() -> list[dict[str, Any]]:
    return [
        {"name": "empty", "state": "", "actor": "10001"},
        {"name": "first", "state": "study the original red notebook", "actor": "10001"},
        {"name": "unchanged", "state": "study the original red notebook", "actor": "10001"},
        {"name": "cold_after_unchanged", "state": None, "actor": "10001", "evict": True},
        {"name": "change", "state": "study the original blue notebook", "actor": "10001"},
        {"name": "return_A", "state": "study the original red notebook", "actor": "10001"},
        {"name": "clear", "state": "", "actor": "10001"},
        {"name": "before_expiry", "state": "a temporary useful clue", "actor": "10001"},
        {"name": "expired", "state": None, "expire": True, "actor": "10001"},
        {"name": "different_actor", "state": "a shared useful clue", "actor": "10002"},
    ]


def settings_for(database_url: str, root: Path):
    from tests.conftest import make_settings

    persona = root / "synthetic-persona.md"
    persona.write_text(
        "You are Yuki in an isolated synthetic read-only experiment.", encoding="utf-8"
    )
    return make_settings(
        database_url,
        bot_persona_file=persona,
        system_prompt="Read the synthetic request; return a brief internal answer without tools.",
        enabled_groups_csv="20001",
        runtime_work_enabled=True,
        plugin_system_enabled=False,
        agent_max_model_requests=4,
        llm_model="gemini-3.8-flash",
        workspace_directory=root / "workspace",
        conversation_media_cache_directory=root / "media",
        social_transfer_directory=root / "social-transfer",
        sandbox_socket=root / "absent-sandbox.sock",
        web_mode="disabled",
        web_search_bridge_state_path=root / "search.sqlite3",
        emoji_storage_root=root / "emoji",
        model_profiles_file=root / "absent-profiles.toml",
    )


async def seed(root: Path, plan: dict[str, Any]) -> None:
    from dataclasses import asdict

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import update
    from tests.conftest import build_harness
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.conversation.observation_models import (
        ContextObservationModel,
        ContextSelectionModel,
    )
    from qq_ai_bot.conversation.observations import encode
    from qq_ai_bot.identity.canonical_repository import ensure_person
    from qq_ai_bot.identity.write_settings import configure_identity_write_settings
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.services.main_agent_turns import _hash
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    database = Database(f"sqlite+aiosqlite:///{(root / 'seed.sqlite3').as_posix()}")
    settings = settings_for(database.url, root)
    os.environ["DATABASE_URL"] = database.url
    os.environ["BOT_PERSONA_FILE"] = str(settings.bot_persona_file)
    cfg = Config()
    cfg.set_main_option("script_location", str(root.parent / "candidate-source" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database.url)
    await asyncio.to_thread(command.upgrade, cfg, "0107")
    configure_identity_write_settings(settings_for(database.url, root))
    env = await social_env(database, root)
    async with database.immediate_session() as writer:
        await ensure_person(writer, "10002", display_name="synthetic second member")
        await writer.execute(
            update(ChatEventModel).values(occurred_at=datetime(2026, 10, 12, 1, 0, tzinfo=UTC))
        )
    harness = build_harness(database, settings_for(database.url, root))
    await harness.profiles.observe(user_id="10002", nickname="second member", group_id="20001")
    chat = harness.processor._chat
    contract = MainAgentContract(chat, ShortState(WorkspaceStore(root / "state")))
    tools = plan.get("tools") or [asdict(tool) for tool in await contract.definitions()]
    read_scope = json.dumps(
        {"memory": [], "plugin_id": None, "delegation_id": None}, sort_keys=True
    )
    view_key = _hash(
        ["main-history-v2", env.context.conversation_id, "main", env.person, read_scope, True]
    )
    fixture_ids = {
        key: f"00000000-0000-4000-8000-{index:012d}"
        for index, key in enumerate(("old", "auto", "note", "mixed", "old_uncovered"), 1)
    }
    payloads = {
        "old": {
            "kind": "dynamic_snapshot",
            "contributions": {
                "context.relationship": {
                    "payload": {
                        "stage": "close",
                        "style": "synthetic retired warm",
                        "unverified_claim_gap": 15,
                    }
                }
            },
        },
        "auto": {
            "version": 1,
            "facts": [
                {
                    "text": "Synthetic retired relation: close/warm/unverified_claim_gap=15",
                    "refs": ["observation:" + fixture_ids["old"]],
                }
            ],
            "unresolved": [],
            "next_steps": [],
        },
        "note": {
            "version": 1,
            "facts": [
                {
                    "text": "SYNTHETIC_REAL_NOTE: the real notebook evidence remains available",
                    "refs": ["goal"],
                }
            ],
            "unresolved": [],
            "next_steps": [],
        },
        "mixed": {
            "version": 1,
            "facts": [
                {
                    "text": "SYNTHETIC_REAL_NOTE: the real notebook evidence remains available",
                    "refs": ["observation:" + fixture_ids["note"]],
                }
            ],
            "unresolved": [],
            "next_steps": [],
        },
    }
    payloads["old_uncovered"] = payloads["old"]
    parents = {
        "old": [],
        "auto": [[fixture_ids["old"], 1]],
        "note": [],
        "mixed": [[fixture_ids["old"], 1], [fixture_ids["note"], 1]],
        "old_uncovered": [],
    }
    async with database.immediate_session() as writer:
        for key in fixture_ids:
            source_key = {
                "old": "snapshot:synthetic-retired",
                "auto": "scope-summary:synthetic-auto",
                "note": "work-note:synthetic-real:1",
                "mixed": "scope-summary:synthetic-mixed",
                "old_uncovered": "snapshot:synthetic-uncovered",
            }[key]
            writer.add(
                ContextObservationModel(
                    id=fixture_ids[key],
                    conversation_id=env.context.conversation_id,
                    generation=1,
                    actor_id=env.person,
                    read_scope=read_scope,
                    source_key=source_key,
                    version=1,
                    privacy_generation=0,
                    payload_json=encode(payloads[key]),
                    parent_sources_json=encode(parents[key]),
                    summary_view_key=view_key if key != "note" else None,
                    created_at=datetime(2026, 10, 12, 1, 1, tzinfo=UTC),
                )
            )
        await writer.flush()
        for key in ("auto", "mixed", "old_uncovered"):
            item = {
                "kind": "model_input",
                "event_ids": [],
                "message": {
                    "role": "user",
                    "content": "[Observation; derived notes]\n" + encode(payloads[key]),
                },
                "observation_id": fixture_ids[key],
                "observation_version": 1,
            }
            encoded = encode(item)
            writer.add(
                ContextSelectionModel(
                    view_key=view_key,
                    conversation_id=env.context.conversation_id,
                    generation=1,
                    actor_id=env.person,
                    read_scope=read_scope,
                    source_key=sha(encoded.encode()),
                    event_ids_json="[]",
                    observation_sources_json=encode([[fixture_ids[key], 1]]),
                    payload_json=encoded,
                    created_at=datetime(2026, 10, 12, 1, 2, tzinfo=UTC),
                )
            )
    system = chat._prompt_composer.static_messages()[0].content
    if plan.get("closed_loop"):
        system = closed_loop_system(system)
    write_json(
        root / "common.json",
        {
            "conversation_id": env.context.conversation_id,
            "person_id": env.person,
            "space_id": env.space,
            "presence_id": env.presence,
            "system": system,
            "tools": tools,
            "retired_fixture_sources": fixture_ids,
            "read_scope": read_scope,
            "view_key": view_key,
        },
    )
    await database.close()


async def run_arm(root: Path, arm: str, plan: dict[str, Any], route=None) -> None:
    import httpx
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import delete, select
    from tests.conftest import MemorySender, build_harness
    from tests.support.automation_runtime_helpers import FakeClock

    from qq_ai_bot.conversation.projection_models import PromptProjectionModel
    from qq_ai_bot.domain.conversations import ScopeType
    from qq_ai_bot.domain.messages import ChatTool, InboundMessage, SenderIdentity
    from qq_ai_bot.execution_trace.payload import decode_payload, encode_payload
    from qq_ai_bot.model_runtime.executor import TaskModelExecutor
    from qq_ai_bot.model_runtime.models import ModelProfile
    from qq_ai_bot.model_runtime.pool import ModelClientPool
    from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
    from qq_ai_bot.model_runtime.routes import ModelRouter
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.prompting.contributors import static_text
    from qq_ai_bot.prompting.models import PromptChannel
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.services.prompt_composer import PromptComposer
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    common = json.loads((root.parent / "seed" / "common.json").read_text(encoding="utf-8"))
    with sqlite3.connect(root.parent / "seed" / "seed.sqlite3") as original:
        with sqlite3.connect(root / "replay.sqlite3") as destination:
            original.backup(destination)
    database = Database(f"sqlite+aiosqlite:///{(root / 'replay.sqlite3').as_posix()}")
    candidate_source = Path(plan.get("frozen_candidate_root", root.parent / "candidate-source"))
    if arm != "A":
        settings = settings_for(database.url, root)
        os.environ["DATABASE_URL"] = database.url
        os.environ["BOT_PERSONA_FILE"] = str(settings.bot_persona_file)
        cfg = Config()
        cfg.set_main_option("script_location", str(candidate_source / "migrations"))
        cfg.set_main_option("sqlalchemy.url", database.url)
        await asyncio.to_thread(command.upgrade, cfg, "0108")
    profile = route[0] if route else ModelProfile.model_validate(plan["profile"])
    probe = probe_helpers(candidate_source / "tools" / "cache_probe.py")
    physical = probe.PhysicalRequests(profile.protocol)
    bodies = []
    sender = MemorySender()
    scripted_calls = {}
    previous_answer = None
    step_answer = None

    async def observe_request(request):
        await physical.request(request)
        physical.rows[-1].update(
            started_utc=datetime.now(UTC).isoformat(),
            profile_id=profile.id,
            provider=profile.provider,
            protocol=profile.protocol.value,
            requested_model=profile.model,
            actual_upstream_account=plan.get("account_label", "offline_synthetic"),
        )
        payload = json.loads(request.content)
        texts, _ = wire_texts(payload)
        physical.rows[-1]["prior_synthetic_messages_in_request"] = sum(
            any(message.text in text_value for text_value in texts) for message in sender.messages
        )
        physical.rows[-1]["controller_previous_answer_in_request"] = (
            any(previous_answer in text_value for text_value in texts)
            if previous_answer is not None
            else False
        )
        if route:
            encoded = encode_payload(payload, sys.maxsize)
            diagnostic = decode_payload(
                encoded.compressed, size=encoded.size, digest=encoded.sha256
            )
            bodies.append(json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")))
            physical.rows[-1]["wire_record_status"] = encoded.status
            physical.rows[-1]["opaque_redactions"] = diagnostic["redactions"]
        else:
            bodies.append(request.content.decode("utf-8"))
        async with database.sessions() as reader:
            saved = await reader.scalar(
                select(PromptProjectionModel).order_by(PromptProjectionModel.updated_at.desc())
            )
        if saved is not None:
            physical.rows[-1]["projection"] = {
                "view_key": saved.view_key,
                "generation": saved.generation,
                "epoch": saved.epoch_id,
                "source_revision": saved.source_revision,
                "selected_fragments": json.loads(saved.payload_json),
                "visible_material": json.loads(getattr(saved, "visible_material_json", "{}")),
            }
        write_json(root / "physical.json", physical.rows)

    async def observe_response(response):
        await physical.response(response)
        physical.rows[-1]["finished_utc"] = datetime.now(UTC).isoformat()
        if physical.rows[-1]["response_shape"] != profile.protocol.value + "_object":
            physical.rows[-1].update(original_numeric_usage=None, usage_presence=None)
            write_json(root / "physical.json", physical.rows)
            return
        payload = response.json()
        physical.rows[-1].update(
            response_metadata(payload, profile.protocol.value, real=bool(route))
        )
        write_json(root / "physical.json", physical.rows)

    def respond(request):
        parts = [{"text": "Synthetic internal answer; no tools or QQ effects."}]
        if plan.get("closed_loop"):
            step = physical.sample["step"]
            calls = scripted_calls.get(step, 0)
            scripted_calls[step] = calls + 1
            if calls == 0:
                parts = [
                    {
                        "functionCall": {
                            "name": "get_my_capabilities",
                            "args": {"mode": "summary"},
                        }
                    }
                ]
            else:
                state_text = plan["scenarios"][step]["state"] or "cleared"
                parts = [
                    {
                        "text": f"SYNTHETIC_INTERNAL_{step:02}: {state_text}; "
                        "SYNTHETIC_REAL_NOTE: the real notebook evidence remains available."
                    }
                ]
        if profile.protocol.value == "gemini":
            payload = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": parts,
                        },
                    }
                ],
            }
        elif profile.protocol.value == "chat_completions":
            call = parts[0].get("functionCall")
            message = {"role": "assistant", "content": parts[0].get("text", "")}
            if call:
                message["tool_calls"] = [
                    {
                        "id": f"synthetic-call-{step}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                    }
                ]
                message["reasoning_content"] = f"Synthetic reasoning for step {step}."
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if call else "stop",
                        "message": message,
                    }
                ]
            }
        else:
            call = parts[0].get("functionCall")
            output = (
                [
                    {
                        "type": "function_call",
                        "call_id": f"synthetic-call-{step}",
                        "name": call["name"],
                        "arguments": json.dumps(call["args"]),
                    }
                ]
                if call
                else [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": parts[0]["text"], "annotations": []}
                        ],
                    }
                ]
            )
            payload = {
                "id": f"synthetic-response-{physical.sample['step']}-{len(physical.rows)}",
                "object": "response",
                "status": "completed",
                "output": output,
            }
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        base_url=profile.base_url.rstrip("/") + "/",
        transport=None if route else httpx.MockTransport(respond),
        timeout=profile.timeout_seconds,
        headers=profile.headers,
        event_hooks={"request": [observe_request], "response": [observe_response]},
    )
    provider = probe.build_provider(
        profile if route else profile.model_copy(update={"max_retries": 0}),
        route[1] if route else "synthetic-key",
        client,
    )
    executor = TaskModelExecutor(
        router=ModelRouter(probe.profile_catalog(profile)),
        pool=ModelClientPool(injected_profiles={profile.id: provider}),
        invocations=ModelInvocationRepository(database),
    )
    original_execute = executor._execute
    logical_index = 0

    async def tracked_execute(task, request, **options):
        nonlocal logical_index, step_answer
        logical_index += 1
        physical.sample.update(
            logical_index=logical_index,
            task=task.value,
            request_chain_id=request.request_chain_id,
        )
        try:
            response = await original_execute(task, request, **options)
            step_answer = response.content
            return response
        finally:
            physical.finish_logical_call(None)

    executor._execute = tracked_execute
    # These are shared experiment controls, never changes to product configuration.
    PromptComposer._static_contributions = lambda self: (
        static_text(
            "experiment.common", common["system"], channel=PromptChannel.INVARIANT, priority=100
        ),
    )
    if arm == "B":
        import qq_ai_bot.services.prompt_composer as module

        original_change = module._material_change
        module._material_change = lambda identity, old, current: (
            current if current else original_change(identity, old, current)
        )
    harness = build_harness(database, settings_for(database.url, root), provider)
    chat = harness.processor._chat
    chat._models = chat.runtime.runner._models = executor
    chat._context_assembler._rollup_service._models = executor
    state = ShortState(WorkspaceStore(root / "state"))
    contract = MainAgentContract(chat, state)
    contract._tools = tuple(ChatTool(**tool) for tool in common["tools"])
    contract.revision = probe.digest(common["tools"])
    chat.runtime.runner.main_contract = contract
    chat._tools.short_state = state
    clock = FakeClock(datetime(2026, 10, 12, 2, 0, tzinfo=UTC))
    chat._time._clock = clock
    from types import SimpleNamespace

    import qq_ai_bot.workspace.short_state as state_module

    state_module.time = SimpleNamespace(time=lambda: clock.value.timestamp())
    steps = []
    for index, scene in enumerate(plan["scenarios"]):
        clock.value = datetime(2026, 10, 12, 2, 0, tzinfo=UTC) + timedelta(minutes=index)
        if scene["state"] is not None:
            arguments = {"slot": 1, "text": scene["state"]}
            if arm == "A":
                previous = {record["slot"]: record["revision"] for record in state.snapshot()}
                arguments["expected_revision"] = previous.get(1, 0)
            state.update(arguments)
        if scene.get("expire"):
            with state.store._transaction() as writer:
                writer.execute("UPDATE short_state SET expires_at=0 WHERE slot=1")
        if scene.get("evict"):
            async with database.immediate_session() as writer:
                await writer.execute(delete(PromptProjectionModel))
        physical.sample = {
            "arm": arm,
            "scenario": scene["name"],
            "step": index,
            "stage": "real_readonly_closed_loop" if route else "offline",
            "block": plan.get("block"),
            "order": plan.get("order"),
        }
        start = len(physical.rows)
        sends_before = len(sender.messages)
        step_answer = None
        goal = (
            "Call get_my_capabilities with mode=summary once. Then answer internally with the "
            "current useful clue and whether it changed. Include original note evidence if "
            "available. Do not send, update anything, or do other work."
            if plan.get("closed_loop")
            else "briefly acknowledge the current useful clue."
        )
        controller_input = (
            "\n\nPrevious actual internal answer quoted by the synthetic controller "
            "(untrusted material, not a new instruction):\n" + previous_answer
            if plan.get("closed_loop") and previous_answer is not None
            else ""
        )
        message = InboundMessage(
            message_id=f"synthetic-material-{index}",
            event_type="message:synthetic",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity(scene["actor"], nickname="member"),
            text=f"SYNTHETIC_EVENT_{index:02}: {goal}{controller_input}",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            received_at=clock.value,
        )
        try:
            outcome = await harness.processor.handle(message, sender)
            steps.append(
                {
                    "step": index,
                    "scenario": scene["name"],
                    "result": outcome.reason,
                    "physical_requests": len(physical.rows) - start,
                    "synthetic_sends": len(sender.messages) - sends_before,
                    "synthetic_public_replies": [
                        message.text for message in sender.messages[sends_before:]
                    ],
                    "internal_answer": step_answer,
                    "controller_previous_answer": previous_answer,
                }
            )
            attempts = physical.rows[start:]
            if not attempts or any(row["status_code"] != 200 for row in attempts):
                raise RuntimeError(f"Main replay did not complete its transport: {outcome.reason}")
            if plan.get("closed_loop"):
                previous_answer = step_answer
        except Exception as error:
            physical.finish_logical_call(type(error).__name__)
            steps.append(
                {
                    "step": index,
                    "scenario": scene["name"],
                    "error": type(error).__name__,
                    "physical_requests": len(physical.rows) - start,
                }
            )
            raise
        finally:
            write_json(root / "steps.json", steps)
            write_json(root / "physical.json", physical.rows)
            (root / ("body-diagnostics.jsonl" if route else "bodies.jsonl")).write_text(
                "\n".join(bodies) + "\n", encoding="utf-8"
            )
    write_json(root / "totals.json", usage_totals(physical.rows))
    write_json(
        root / "effects.json",
        {
            "qq_or_business_transport_started": False,
            "synthetic_sender_messages": len(sender.messages),
            "public_replies": [message.text for message in sender.messages],
            "controller_quotes_actual_internal_answers": bool(plan.get("closed_loop")),
        },
    )
    await executor.close()
    await client.aclose()
    await database.close()


async def live_closed_loop(args, profile, api_key) -> None:
    """Each arm owns its actual internal answers and read-only protocol continuation."""
    original = json.loads((args.replay / "plan.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    seed_root = args.output / "seed"
    seed_root.mkdir()
    with sqlite3.connect(args.replay / "seed" / "seed.sqlite3") as source:
        with sqlite3.connect(seed_root / "seed.sqlite3") as destination:
            source.backup(destination)
    common = json.loads((args.replay / "seed" / "common.json").read_text(encoding="utf-8"))
    common["system"] = closed_loop_system(common["system"])
    write_json(seed_root / "common.json", common)
    candidate = (args.replay / "candidate-source").resolve()
    plan = {
        **original,
        "mode": "real_readonly_closed_loop_actual_main_serializer",
        "closed_loop": True,
        "shared_controls": [
            "same canonical seed backup",
            "same fixed candidate static contract",
            "same ordered candidate tools",
            "same task goals/event clocks/state/actor sequence",
            "each arm owns its actual model responses and native continuation",
        ],
        "pending_scenarios": [
            "artifact and private Work dependencies beyond the synthetic mixed-note fixture",
            "actual process restart",
            "SELF/automation/shared plugins/media/native tools",
            "same-event attempt and source CAS races",
            "public QQ/natural traffic and the original send continuation failure",
        ],
        "real_numeric_usage_measured": False,
        "complete_cash_bill_measured": False,
        "scenarios": [
            scene
            for scene in original["scenarios"]
            if scene["name"] in {"first", "unchanged", "change", "clear"}
        ],
        "profile": profile_data(profile),
        "captured_initial_wire_generation_by_arm": captured_generation(args.replay),
        "generation_semantics": (
            "Profile is route/default configuration; Main's actual ChatRequest can override it. "
            "Captured wire controls are separate, without instructions or schemas."
        ),
        "account_label": args.account_label,
        "frozen_candidate_root": str(candidate),
        "source_run": str(args.replay.resolve()),
        "experiment_hook_sha256": sha(await asyncio.to_thread(Path(__file__).read_bytes)),
        "started_utc": datetime.now(UTC).isoformat(),
        "order": args.order,
        "warmups": 0,
        "upstream_wire_verified": False,
        "server_cache_cleared": False,
        "QQ_or_external_business_effects": False,
        "controller": "quote this arm's actual internal answer into its next synthetic inbound",
        "wire_copy": (
            "existing trace payload codec omits opaque signatures; exact raw byte count/hash "
            "recorded, diagnostic copy is not signature replay"
        ),
    }
    del plan["cache_cost_or_real_usage_measured"]
    write_json(args.output / "manifest.json", plan)
    private_pipe = json.dumps(
        {"profile": profile_data(profile, private_headers=True), "api_key": api_key}
    )
    rows = []
    for block, order in enumerate(args.order.split(",")):
        for arm in order:
            destination = args.output / f"block-{block}-{arm}"
            destination.mkdir()
            write_json(args.output / "plan.json", {**plan, "block": block, "order": order})
            selected = Path(original["baseline_source"]) if arm == "A" else candidate
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        __file__,
                        "--child",
                        arm,
                        "--live",
                        "--source-root",
                        str(selected),
                        "--output",
                        str(destination.resolve()),
                    ],
                    cwd=selected,
                    input=private_pipe,
                    text=True,
                    check=True,
                )
            finally:
                child_rows = destination / "physical.json"
                if child_rows.exists():
                    rows.extend(json.loads(child_rows.read_text(encoding="utf-8")))
                    plan["real_numeric_usage_measured"] = any(
                        row.get("original_numeric_usage") for row in rows
                    )
                    write_json(args.output / "manifest.json", plan)
                    write_json(args.output / "physical.json", rows)
                    write_json(args.output / "totals.json", usage_totals(rows))
    print(
        json.dumps(
            {
                "mode": plan["mode"],
                "output": str(args.output.resolve()),
                "physical_requests": len(rows),
                "real_qq_effects": 0,
            }
        )
    )


async def live_replay(args) -> None:
    import httpx
    from tools.cache_probe import (
        PhysicalRequests,
        build_provider,
        endpoint_origin,
        route_from_stdin,
    )

    from qq_ai_bot.domain.messages import ChatRequest

    profile, api_key = route_from_stdin(sys.stdin)
    plan = json.loads((args.replay / "plan.json").read_text(encoding="utf-8"))
    if (
        any(getattr(profile, key) != plan["profile"][key] for key in ("provider", "model"))
        or profile.protocol.value != plan["profile"]["protocol"]
    ):
        raise ValueError("actual route must match the captured provider, protocol and model")
    if args.closed_loop:
        await live_closed_loop(args, profile, api_key)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    physical = PhysicalRequests(profile.protocol)
    started = datetime.now(UTC).isoformat()

    async def observe_request(request):
        await physical.request(request)
        physical.rows[-1].update(
            started_utc=datetime.now(UTC).isoformat(),
            requested_model=profile.model,
            provider=profile.provider,
            protocol=profile.protocol.value,
            profile_id=profile.id,
            upstream_account=args.account_label,
        )
        write_json(args.output / "physical.json", physical.rows)

    async def observe_response(response):
        await physical.response(response)
        physical.rows[-1]["finished_utc"] = datetime.now(UTC).isoformat()
        if physical.rows[-1]["response_shape"] != profile.protocol.value + "_object":
            physical.rows[-1].update(
                original_numeric_usage=None,
                usage_presence=None,
                metering_kind="unknown_non_object_response",
            )
            write_json(args.output / "physical.json", physical.rows)
            return
        payload = response.json()
        physical.rows[-1].update(
            **response_metadata(payload, profile.protocol.value, real=True),
            upstream_account=args.account_label,
            complete_search_storage_cost=None,
        )
        write_json(args.output / "physical.json", physical.rows)

    async with httpx.AsyncClient(
        base_url=profile.base_url.rstrip("/") + "/",
        timeout=profile.timeout_seconds,
        headers=profile.headers,
        event_hooks={"request": [observe_request], "response": [observe_response]},
    ) as client:
        provider = build_provider(profile, api_key, client)
        endpoint = provider._path(ChatRequest(model=profile.model, messages=()))
        request_headers = {**provider._request_headers(), "content-type": "application/json"}
        for block, order in enumerate(args.order.split(",")):
            for arm in order:
                bodies = (
                    (args.replay / arm / "bodies.jsonl").read_text(encoding="utf-8").splitlines()
                )
                if args.limit is not None:
                    bodies = bodies[: args.limit]
                sequence = [("warmup", 0, bodies[0])] * args.warmups + [
                    ("measured", index, body) for index, body in enumerate(bodies)
                ]
                for stage, index, body in sequence:
                    physical.sample = {
                        "arm": arm,
                        "block": block,
                        "order": order,
                        "stage": stage,
                        "sample_index": index,
                    }
                    try:
                        response = await client.post(
                            endpoint,
                            content=body.encode(),
                            headers=request_headers,
                        )
                        if response.is_error:
                            physical.rows[-1]["error_category"] = "http_error"
                        physical.finish_logical_call(None)
                    except httpx.HTTPError as error:
                        physical.finish_logical_call(type(error).__name__)
                    finally:
                        write_json(args.output / "physical.json", physical.rows)
                    if args.delay_seconds:
                        await asyncio.sleep(args.delay_seconds)
    write_json(args.output / "totals.json", usage_totals(physical.rows))
    write_json(
        args.output / "manifest.json",
        {
            "started": started,
            "mode": "fixed_public_input_real_api_replay",
            "profile_id": profile.id,
            "provider": profile.provider,
            "model": profile.model,
            "protocol": profile.protocol.value,
            "endpoint_origin": endpoint_origin(profile.base_url),
            "actual_upstream_account": args.account_label,
            "order": args.order,
            "warmups_per_arm_block": args.warmups,
            "measured_request_limit_per_arm_block": args.limit,
            "upstream_wire_verified": False,
            "server_cache_cleared": False,
            "source_run": str(args.replay.resolve()),
            "closed_loop_or_QQ_acceptance": False,
            "captured_wire_generation_by_arm": captured_generation(args.replay),
            "generation_semantics": (
                "fixed replay sends the captured body; actual generation is its wire shape, "
                "separate from Profile defaults"
            ),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--candidate-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tools-manifest", type=Path)
    parser.add_argument(
        "--profile-json", type=Path, help="public profile settings for offline serializer controls"
    )
    parser.add_argument(
        "--replay", type=Path, help="existing offline A/B/C run for fixed-input live replay"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--closed-loop",
        action="store_true",
        help=(
            "actual Main internal answer feeds the next synthetic inbound after a read-only "
            "get_my_capabilities continuation; offline mode uses fixed scripted responses"
        ),
    )
    parser.add_argument("--route-stdin", action="store_true")
    parser.add_argument(
        "--account-label",
        default="unknown",
        help="verified anonymous upstream account label, if available",
    )
    parser.add_argument("--order", default="ABC,CBA")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=0)
    parser.add_argument("--limit", type=int, help="fixed live measured requests per arm/block")
    parser.add_argument("--child", choices=("seed", "A", "B", "C"), help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.ERROR)
    if args.child:
        sys.path[:0] = [str(args.source_root / "src"), str(args.source_root)]
        plan = json.loads((args.output.parent / "plan.json").read_text(encoding="utf-8"))
        route = (
            probe_helpers(
                Path(plan["frozen_candidate_root"]) / "tools" / "cache_probe.py"
            ).route_from_stdin(sys.stdin)
            if args.live
            else None
        )
        asyncio.run(
            seed(args.output, plan)
            if args.child == "seed"
            else run_arm(args.output, args.child, plan, route)
        )
        return
    sys.path.insert(0, str(args.candidate_root))
    sys.path.insert(0, str(args.candidate_root / "src"))
    if args.live:
        if not args.route_stdin or args.replay is None:
            parser.error("--live requires --route-stdin and an existing --replay directory")
        if (
            args.warmups < 0
            or (args.limit is not None and args.limit < 1)
            or args.delay_seconds < 0
            or any(sorted(order) != list("ABC") for order in args.order.split(","))
        ):
            parser.error(
                "balanced orders must each contain A/B/C once; warmups and delay cannot be negative"
            )
        asyncio.run(live_replay(args))
        return
    if args.baseline_root is None or args.replay is not None:
        parser.error(
            "offline Main replay requires --baseline-root; live replay is separately explicit"
        )
    args.output.mkdir(parents=True, exist_ok=False)
    from qq_ai_bot.model_runtime.models import ModelProfile

    if args.route_stdin and args.profile_json:
        parser.error("select one offline profile source: --route-stdin or --profile-json")
    public_profile = (
        profile_data(
            probe_helpers(args.candidate_root / "tools" / "cache_probe.py").route_from_stdin(
                sys.stdin
            )[0]
        )
        if args.route_stdin
        else json.loads(args.profile_json.read_text(encoding="utf-8"))
        if args.profile_json
        else {
            "id": "synthetic-materials",
            "provider": "gemini",
            "protocol": "gemini",
            "base_url": "https://synthetic.invalid/v1beta",
            "api_key_env": "SYNTHETIC_KEY",
            "model": "gemini-3.8-flash",
            "timeout_seconds": 10,
            "max_retries": 0,
            "default_temperature": 0.7,
            "default_max_output_tokens": 2048,
            "capabilities": ["tools", "reasoning", "structured_output"],
        }
    )
    profile = ModelProfile.model_validate(public_profile)
    public_profile = profile_data(profile)
    frozen = args.output / "candidate-source"
    source = freeze_source(args.candidate_root, frozen)
    baseline = verify_baseline(args.baseline_root, args.candidate_root)
    plan = {
        "schema_version": 1,
        "mode": (
            "offline_fixed_response_readonly_closed_loop"
            if args.closed_loop
            else "offline_fixed_response_actual_main_serializer"
        ),
        "baseline_source": str(args.baseline_root.resolve()),
        "baseline_commit": "24da979dd32b4c38922b48e682effddf3955e027",
        "baseline_verification": baseline,
        "candidate_source": source,
        "profile": public_profile,
        "scenarios": [
            scene
            for scene in scenarios()
            if scene["name"] in {"first", "unchanged", "change", "clear"}
        ]
        if args.closed_loop
        else scenarios(),
        "closed_loop": args.closed_loop,
        "shared_controls": [
            "same canonical seed backup",
            "same fixed candidate static contract",
            "same ordered candidate tools",
            "same event clocks/state/actor sequence",
            "same fixed responses",
        ],
        "excluded_deployment_boundaries": [
            "A04 original vs candidate tool schema",
            "A21 original vs candidate static entity instruction",
        ],
        "shared_schema": "same Alembic 0107 seed; B/C run actual candidate 0108 retirement",
        "pending_scenarios": [
            "artifact and private Work dependencies beyond the synthetic mixed-note fixture",
            "actual process restart; isolated cache row eviction is covered separately",
            "SELF/automation/shared plugins",
            "media/native tools",
            "same-event attempt and source CAS races",
            "real closed loop and QQ",
        ],
        "cache_cost_or_real_usage_measured": False,
    }
    if args.tools_manifest:
        manifest = json.loads(args.tools_manifest.read_text(encoding="utf-8"))
        plan["tools"] = manifest["tools"] if isinstance(manifest, dict) else manifest
    write_json(args.output / "plan.json", plan)
    script = str(Path(__file__).resolve())
    for arm in ("seed", "A", "B", "C"):
        destination = args.output / arm
        destination.mkdir()
        selected_source = args.baseline_root if arm == "A" else frozen
        subprocess.run(
            [
                sys.executable,
                script,
                "--child",
                arm,
                "--source-root",
                str(selected_source.resolve()),
                "--output",
                str(destination.resolve()),
            ],
            cwd=selected_source,
            check=True,
        )
    write_json(
        args.output / "summary.json",
        {
            arm: {
                "physical": json.loads(
                    (args.output / arm / "totals.json").read_text(encoding="utf-8")
                ),
                "steps": json.loads((args.output / arm / "steps.json").read_text(encoding="utf-8")),
            }
            for arm in "ABC"
        },
    )
    plan["actual_wire_generation_by_arm"] = captured_generation(args.output)
    plan["generation_semantics"] = (
        "Profile is route/default configuration; actual Main ChatRequest controls are captured "
        "from the wire separately"
    )
    write_json(args.output / "plan.json", plan)
    metrics = measure_bodies(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "mode": plan["mode"],
                "arms": "ABC",
                "real_api_requests": 0,
                "wire_bytes": {
                    arm: metrics["arms"][arm]["totals"]["wire_body_bytes"] for arm in "ABC"
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
