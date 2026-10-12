"""Offline Main replay using recorded contexts and a read-only production snapshot.

Exactly ff0dbdf is compared with frozen current source, on disposable DB copies.
No live/collect transport, credential input, model invocation or QQ send exists.
Recorded usage is historical; generated bodies have no measured tokens or price.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, replace
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BASELINE = "ff0dbdf10ef27c4af30683ca72cd9bd5f1836270"
OLD_HEADER = "本轮运行资料（按 trust 字段区分可信度）："
PRIVATE_TABLES = (
    "runtime_work",
    "runtime_work_journal",
    "runtime_work_effects",
    "runtime_work_inputs",
    "runtime_protocol_objects",
    "runtime_protocol_refs",
    "runtime_protocol_usage",
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: Any) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else canonical(value).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[Any]) -> None:
    path.write_text("".join(canonical(row) + "\n" for row in rows), encoding="utf-8")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate original numeric fields on their known subset; never fill nulls."""
    fields = {}
    for name in ("prompt_tokens", "cached_prompt_tokens", "completion_tokens", "total_tokens"):
        known = [row[name] for row in rows if type(row.get(name)) is int and row[name] >= 0]
        fields[name] = {
            "known_responses": len(known),
            "missing_or_invalid_responses": len(rows) - len(known),
            "known_subtotal": sum(known) if known else None,
            "complete_total": sum(known) if known and len(known) == len(rows) else None,
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
        "successful_responses": len(rows),
        "fields": fields,
        "cache_eligible_responses": len(eligible),
        "cache_eligible_input": incoming if eligible else None,
        "cache_eligible_cached": cached if eligible else None,
        "weighted_cache_ratio": cached / incoming if incoming else None,
        "explicit_zero_cached_responses": sum(row["cached_prompt_tokens"] == 0 for row in eligible),
        "cache_unknown_or_invalid_responses": len(rows) - len(eligible),
        "new_paid_requests": 0,
        "complete_cost": None,
        "actual_bill": None,
    }


def wire_units(body: dict[str, Any]) -> list[dict[str, Any]]:
    if "contents" in body:
        return body["contents"]
    if "messages" in body:
        return body["messages"]
    return body.get("input", [])


def fixed_wire(body: dict[str, Any], protocol: str) -> dict[str, Any]:
    value = deepcopy(body)
    if protocol == "gemini":
        value.pop("contents")
    elif protocol in {"claude", "chat_completions"}:
        messages = value.pop("messages")
        value["fixed_system_messages"] = [
            message for message in messages if message.get("role") in {"system", "developer"}
        ]
    else:
        value.pop("input")
    return value


def native_items(value: Any, path: str = "$") -> list[dict[str, Any]]:
    """Inventory original carrier order without interpreting or rewriting values."""
    result = []
    if isinstance(value, dict):
        for key, field in value.items():
            current = f"{path}.{key}"
            if (
                key
                in {
                    "thoughtSignature",
                    "signature",
                    "inlineData",
                    "fileData",
                    "image_url",
                    "functionCall",
                    "functionResponse",
                    "tool_calls",
                    "tool_call_id",
                    "response_item",
                    "encrypted_content",
                    "redacted_thinking",
                }
                and field
            ):
                result.append({"path": current, "carrier": key, "sha256": sha(field)})
            result.extend(native_items(field, current))
    elif isinstance(value, list):
        for index, field in enumerate(value):
            result.extend(native_items(field, f"{path}[{index}]"))
    return result


def recorded_evidence(path: Path, output: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    contexts = [row for row in rows if row["record_type"] == "model_context"]
    starts = [item for row in contexts for item in row.get("linked_provider_starts", [])]
    responses = [
        item
        for start in starts
        for item in start.get("provider_events", [])
        if item["metadata"]["kind"] == "provider_response"
    ]
    responses.extend(
        row
        for row in rows
        if row["record_type"] == "unlinked_trace" and row["metadata"]["kind"] == "provider_response"
    )
    responses = list({item["metadata"]["id"]: item for item in responses}.values())
    successful = [item for item in responses if 200 <= item["data"]["http_status"] < 300]
    raw_usage = [
        {
            "trace_id": item["metadata"]["id"],
            "raw_usage_numeric": item["data"]["raw_usage_numeric"],
            "raw_usage_present": item["data"]["raw_usage_present"],
            "normalized_usage": item["data"]["normalized_usage"],
        }
        for item in responses
    ]
    records, previous, append_pairs = [], {}, []
    for row in sorted(
        contexts, key=lambda value: (value["metadata"]["created_at"], value["metadata"]["id"])
    ):
        meta, payload = row["metadata"], row["payload"]
        request = payload["data"]["request"]
        record = {
            "trace_id": meta["id"],
            "execution_id": meta["execution_id"],
            "work_id": meta["work_id"],
            "conversation_id": meta["conversation_id"],
            "generation": meta["generation"],
            "source_event_id": meta["source_event_id"],
            "neutral_sha256": sha(request),
            "neutral_messages": len(request["messages"]),
            "integrity": row["integrity"],
            "redactions": payload["redactions"],
            "native_carriers": native_items(request),
            "actual_public_view_proven": row.get("unique_actual_request_view_proven", False),
            "prepared": [],
        }
        for start in row.get("linked_provider_starts", []):
            data = start["payload"]["data"]
            body = data["body"]
            record["prepared"].append(
                {
                    "trace_id": start["metadata"]["id"],
                    "protocol": data["protocol"],
                    "body_sha256": sha(body),
                    "body_bytes": len(canonical(body).encode()),
                    "redactions": start["payload"]["redactions"],
                    "integrity": start["integrity"],
                    "native_carriers": native_items(body),
                    "prepared_is_not_dispatch_receipt": start["start_is_prepared_not_dispatched"],
                }
            )
            key = (meta["execution_id"], meta["work_id"], data["protocol"])
            if meta["execution_id"] and key in previous:
                old_meta, old_body = previous[key]
                old_units, units = wire_units(old_body), wire_units(body)
                common = 0
                for old, new in zip(old_units, units, strict=False):
                    if old != new:
                        break
                    common += 1
                append_pairs.append(
                    {
                        "execution_id": meta["execution_id"],
                        "work_id": meta["work_id"],
                        "before_trace_id": old_meta["id"],
                        "after_trace_id": meta["id"],
                        "before_items": len(old_units),
                        "after_items": len(units),
                        "exact_common_items": common,
                        "exact_append": common == len(old_units),
                        "signature_bytes_redacted": bool(start["payload"]["redactions"]),
                    }
                )
            previous[key] = meta, body
        records.append(record)
    write_jsonl(output / "recorded-contexts.jsonl", records)
    write_json(output / "recorded-usage.json", raw_usage)
    write_json(output / "recorded-execution-prefixes.json", append_pairs)
    summary = {
        "source": str(path),
        "source_sha256": sha(path.read_bytes()),
        "manifest": rows[0],
        "contexts": len(contexts),
        "prepared_records": len(starts),
        "linked_physical_responses": len(responses),
        "redacted_contexts": sum(bool(row["payload"]["redactions"]) for row in contexts),
        "usage": usage_totals([item["data"]["normalized_usage"] for item in successful]),
        "same_execution_pairs": len(append_pairs),
        "append_pairs": sum(item["exact_append"] for item in append_pairs),
        "public_history_is_inventory_not_proven_dispatch": True,
        "limits": [
            "Original contexts are not requests made by the new layout.",
            "Prepared bodies are not evidence of transport success.",
            "Redacted signatures are inventoried, not restored or sent.",
            "No actor/read_scope is guessed from QQ text or selection membership.",
        ],
    }
    write_json(output / "recorded-summary.json", summary)
    return summary


def database_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    private = {}
    for name in PRIVATE_TABLES:
        if name not in tables:
            continue
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        private[name] = {"rows": len(rows), "sha256": sha(rows)}
    observations = [
        dict(row)
        for row in connection.execute("SELECT * FROM model_context_observations ORDER BY id")
    ]
    notes = [row for row in observations if row["source_key"].startswith("work-note:")]
    automatic = [
        row
        for row in observations
        if row["source_key"].startswith("snapshot:")
        and json.loads(row["payload_json"]).get("kind") == "dynamic_snapshot"
    ]
    prefix_selections = connection.execute(
        "SELECT count(*) FROM model_context_selections "
        "WHERE substr(json_extract(payload_json,'$.message.content'),1,?)=?",
        (len(OLD_HEADER), OLD_HEADER),
    ).fetchone()[0]
    automatic_roots = connection.execute(
        "SELECT count(*) FROM model_context_selections s "
        "JOIN model_context_observations o ON o.id=json_extract(s.payload_json,'$.observation_id') "
        "WHERE o.source_key LIKE 'snapshot:%' "
        "AND json_extract(o.payload_json,'$.kind')='dynamic_snapshot'"
    ).fetchone()[0]
    projections = [
        dict(row)
        for row in connection.execute(
            "SELECT view_key,payload_json,selected_summary_text FROM prompt_projections "
            "ORDER BY view_key"
        )
    ]
    return {
        "schema": connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
        "private": private,
        "context_notes": {"rows": len(notes), "sha256": sha(notes)},
        "automatic_sources": len(automatic),
        "automatic_source_ids": [row["id"] for row in automatic],
        "selections": connection.execute(
            "SELECT count(*) FROM model_context_selections"
        ).fetchone()[0],
        "compiled_material_selections": prefix_selections,
        "public_automatic_roots": automatic_roots,
        "projection_count": len(projections),
        "nonempty_public_projections": sum(
            json.loads(row["payload_json"]) != [] for row in projections
        ),
        "projection_material_occurrences": sum(
            row["payload_json"].count(OLD_HEADER) for row in projections
        ),
    }


def snapshot_controls(snapshot: Path, contexts: Path) -> dict[str, Any]:
    from qq_ai_bot.persistence.repository_helpers import _event_record

    with sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True) as reader:
        reader.row_factory = sqlite3.Row
        ordinary = []
        for scope in reader.execute(
            "SELECT cc.*,la.scope_key FROM canonical_conversations cc "
            "JOIN conversation_legacy_aliases la ON la.id=cc.primary_alias_id "
            "WHERE cc.kind='space' ORDER BY cc.last_event_id DESC"
        ):
            found, actors = [], set()
            for event in reader.execute(
                "SELECT * FROM chat_events WHERE canonical_conversation_id=? AND id>? "
                "AND direction='inbound' AND author_kind='person' AND event_kind='message' "
                "AND (suppression_status IS NULL OR suppression_status='keeper') "
                "ORDER BY id DESC LIMIT 80",
                (scope["id"], scope["starts_after_event_id"]),
            ):
                if event["author_person_id"] in actors:
                    continue
                record = _event_record(dict(event))
                if not record.content.strip():
                    continue
                selected = reader.execute(
                    "SELECT read_scope FROM model_context_selections "
                    "WHERE conversation_id=? AND generation=? AND actor_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (scope["id"], scope["generation"], event["author_person_id"]),
                ).fetchone()
                if selected is None:
                    continue
                actors.add(event["author_person_id"])
                found.append(
                    {"event": dict(event), "scope": dict(scope), "read_scope": selected[0]}
                )
                if len(found) == 2:
                    ordinary = sorted(found, key=lambda item: item["event"]["id"])
                    break
            if ordinary:
                break
        rows = load_jsonl(contexts)
        context = next(
            row
            for row in rows
            if row["record_type"] == "model_context"
            and row["task"] == "chat_agent"
            and not row["payload"]["data"]["request"]["continuation"]
            and row.get("linked_provider_starts")
        )
        provider_start = context["linked_provider_starts"][0]
        prepared = provider_start["payload"]["data"]["body"]
        effective_request = deepcopy(context["payload"]["data"]["request"])
        generation = prepared["generationConfig"]
        thinking = generation.get("thinkingConfig", {})
        effective_request["max_output_tokens"] = generation.get("maxOutputTokens")
        effective_request["thinking_enabled"] = bool(thinking)
        effective_request["reasoning_effort"] = thinking.get("thinkingLevel")
        state = next(
            (row["rows"] for row in rows if row["record_type"] == "current_short_state"), []
        )
        state = [
            {
                key: value
                for key, value in row.items()
                if key in {"slot", "text", "revision", "expires_at"}
            }
            for row in state
            if row.get("visible_at_capture", True) and row.get("text")
        ]
        self_source = reader.execute(
            "SELECT w.id,w.source_json,cc.generation,la.scope_key FROM runtime_work w "
            "JOIN canonical_conversations cc ON cc.id=w.conversation_id "
            "AND cc.generation=w.generation "
            "JOIN conversation_legacy_aliases la ON la.id=cc.primary_alias_id "
            "WHERE json_extract(w.source_json,'$.origin')='self_initiative' "
            "ORDER BY w.updated DESC LIMIT 1"
        ).fetchone()
        return {
            "snapshot": str(snapshot),
            "snapshot_read_uri": snapshot.resolve().as_uri() + "?mode=ro",
            "source_size": snapshot.stat().st_size,
            "original_schema": reader.execute("SELECT version_num FROM alembic_version").fetchone()[
                0
            ],
            "ordinary": ordinary,
            "short_state": state,
            "self": dict(self_source) if self_source else None,
            "request": context["payload"]["data"]["request"],
            "effective_request": effective_request,
            "prepared_body": prepared,
            "request_source": {
                "path": str(contexts),
                "trace_metadata": context["metadata"],
                "prepared_trace_metadata": provider_start["metadata"],
                "prepared_integrity": provider_start["integrity"],
                "protocol": provider_start["payload"]["data"]["protocol"],
                "source_sha256": sha(contexts.read_bytes()),
            },
            "request_to_wire": {
                "neutral_model_alias": effective_request["model"],
                "neutral_temperature": effective_request["temperature"],
                "captured_generation_config": generation,
                "captured_fixed_http_sha256": sha(fixed_wire(prepared, "gemini")),
                "reconstruction_input_sha256": sha(effective_request),
                "semantics": (
                    "effective_request is a neutral serializer reconstruction input, "
                    "with Gemini generation fields taken from its linked prepared body. "
                    "It is not a recovered live Profile or endpoint. Model aliases and "
                    "neutral temperature do not imply fields absent from the prepared wire."
                ),
            },
            "snapshot_inventory": database_inventory(reader),
        }


def freeze_sources(root: Path, output: Path) -> dict[str, Any]:
    archive = output / "baseline.zip"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            "-o",
            str(archive),
            BASELINE,
            "src",
            "tests",
            "migrations",
            "config/persona.md",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=root,
        check=True,
    )
    baseline = output / "baseline-source"
    with zipfile.ZipFile(archive) as reader:
        reader.extractall(baseline)
    candidate = output / "current-source"
    names = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "src",
            "tests",
            "migrations",
            "config/persona.md",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=root,
        text=True,
    ).splitlines()
    fingerprints = {}
    for name in sorted(set(names)):
        original = root / name
        if original.is_file():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
            fingerprints[name] = sha(original.read_bytes())
    dependency = metadata.distribution("yuki-semantic-participation")
    direct = json.loads(dependency.read_text("direct_url.json") or "{}")
    commit = direct.get("vcs_info", {}).get("commit_id")
    archive_commit = re.search(r"/([0-9a-f]{40})\.tar\.gz", direct.get("url", ""))
    if commit is None and archive_commit is not None:
        commit = archive_commit.group(1)
    manifest = {
        "baseline_commit": BASELINE,
        "baseline_archive_sha256": sha(archive.read_bytes()),
        "current_source_sha256": sha(fingerprints),
        "current_files": fingerprints,
        "shared_environment": {
            "python": sys.version,
            "participation_version": dependency.version,
            "participation_installed_commit": commit,
            "baseline_participation_lock_commit": "855f407d61590bcd689e2535c0c30b867560aa66",
            "limits": "Both isolated source processes use the current venv; SP is not invoked.",
        },
    }
    write_json(output / "source-manifest.json", manifest)
    return manifest


async def serialized_bodies(request: Any) -> dict[str, Any]:
    import httpx

    from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
    from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
    from qq_ai_bot.llm.gemini import GeminiProvider
    from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider

    def reject(_request):
        raise AssertionError("Offline composition cannot dispatch HTTP")

    result = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        for protocol, provider_class in (
            ("gemini", GeminiProvider),
            ("claude", AnthropicMessagesProvider),
            ("chat_completions", OpenAICompatibleProvider),
            ("responses", DeepSeekResponsesProvider),
        ):
            provider = provider_class(
                base_url="https://offline.invalid/",
                api_key="unused",
                timeout_seconds=1,
                max_retries=0,
                client=client,
            )
            result[protocol] = provider._build_payload(request)
    return result


async def run_snapshot_arm(args) -> None:
    from alembic import command
    from alembic.config import Config
    from pydantic import TypeAdapter
    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import ChatRequest, ChatTool, InboundMessage, SenderIdentity
    from qq_ai_bot.domain.profiles import UserProfileSnapshot
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.repository_helpers import _event_record
    from qq_ai_bot.prompting.contributors import static_text
    from qq_ai_bot.prompting.models import PromptChannel
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.services.prompt_composer import PromptComposer
    from qq_ai_bot.services.turn_transcript import TranscriptRequest, validating_request

    output = args.output / args.child
    output.mkdir()
    control = json.loads((args.output / "controls.json").read_text(encoding="utf-8"))
    target = output / "snapshot.sqlite3"
    with sqlite3.connect(control["snapshot_read_uri"], uri=True) as reader:
        with sqlite3.connect(target) as writer:
            reader.backup(writer)
    database = Database(f"sqlite+aiosqlite:///{target.as_posix()}")
    os.environ["DATABASE_URL"] = database.url
    cfg = Config()
    cfg.set_main_option("script_location", str(args.source_root / "migrations"))
    await asyncio.to_thread(command.upgrade, cfg, "0108" if args.child == "old" else "0110")
    with sqlite3.connect(target) as reader:
        reader.row_factory = sqlite3.Row
        migrated = database_inventory(reader)
    write_json(
        output / "migration.json",
        {
            "before": control["snapshot_inventory"],
            "after": migrated,
            "private_unchanged": control["snapshot_inventory"]["private"] == migrated["private"],
            "context_notes_unchanged": control["snapshot_inventory"]["context_notes"]
            == migrated["context_notes"],
        },
    )

    def no_model(_request):
        raise AssertionError("Offline composition cannot invoke a model")

    settings = make_settings(database.url, runtime_work_enabled=True, code_mode_enabled=False)
    harness = build_harness(database, settings, FakeLLMProvider(no_model))
    chat = harness.processor._chat
    state = SimpleNamespace(snapshot=lambda: deepcopy(control["short_state"]))
    contract = MainAgentContract(chat, state)
    chat.runtime.runner.main_contract = contract
    actual_tools = await contract.model_definitions()
    actual_system = chat._prompt_composer.static_messages()
    write_json(
        output / "deployment-contract.json",
        {
            "system": [asdict(message) for message in actual_system],
            "tools": [asdict(tool) for tool in actual_tools],
            "revision": contract.revision,
            "persona_setting": "isolated default; recorded system controls the layout comparison",
        },
    )
    common = TypeAdapter(ChatRequest).validate_python(control["effective_request"])
    common_system = next(message.content for message in common.messages if message.role == "system")
    # Explicit experiment controls shared by both source processes and every entry.
    PromptComposer._static_contributions = lambda self: (
        static_text(
            "experiment.recorded_system",
            common_system,
            channel=PromptChannel.INVARIANT,
            priority=100,
        ),
    )
    contract._tools = tuple(ChatTool(**asdict(tool)) for tool in common.tools)
    contract.revision = sha([asdict(tool) for tool in common.tools])
    requests, cases, bodies = [], [], defaultdict(list)

    async def capture(name, context, runtime, inbound=None, read_scope=None):
        composition = await chat.runtime.main_turns.compose(
            inbound=inbound, context=context, runtime=runtime, read_scope=read_scope
        )
        if composition.commit_projection is not None:
            with validating_request(TranscriptRequest(composition.messages, None, ())):
                await composition.commit_projection()
        request = replace(
            common,
            messages=composition.messages,
            continuation=None,
            continuation_items=(),
            conversation_prefix_hash=composition.metrics.conversation_prefix_hash,
            prompt_snapshot_fingerprint=composition.metrics.prompt_snapshot_fingerprint,
            static_prompt_revision=composition.metrics.stable_prefix_hash,
            request_shape_hash="",
            request_chain_id="",
            **(
                {
                    "provider_session_id": (
                        f"yuki:{context.read_version.conversation_id}:"
                        f"g{context.read_version.generation}:main"
                    )
                }
                if args.child == "current"
                else {}
            ),
        )
        prepared = await serialized_bodies(request)
        for protocol, body in prepared.items():
            bodies[protocol].append(body)
        requests.append(TypeAdapter(ChatRequest).dump_python(request, mode="json"))
        cases.append(
            {
                "name": name,
                "source_event_id": context.current_event_id,
                "actor_id": inbound.person_id if inbound else None,
                "read_scope": read_scope,
                "conversation_id": context.read_version.conversation_id,
                "generation": context.read_version.generation,
                "session_control": (
                    "Manual wire control from the actual canonical/generation; "
                    "this compose-only case has no activated Work."
                    if args.child == "current"
                    else "The baseline had no declared provider session metadata."
                ),
                "original_current_sha256": sha(asdict(context.current_message)),
                "raw_history_sha256": sha(
                    [asdict(message) for message in context.history_messages]
                ),
                "raw_history_events": sorted(context.visible_event_ids),
                "dynamic_characters": composition.metrics.dynamic_characters,
                "body": {
                    protocol: {"sha256": sha(body), "bytes": len(canonical(body).encode())}
                    for protocol, body in prepared.items()
                },
            }
        )
        return composition

    ordinary_inputs = []
    for index, original in enumerate(control["ordinary"]):
        event, scope = original["event"], original["scope"]
        record = _event_record(event)
        identity = ConversationScope.group(record.bot_user_id, record.group_id)
        inbound = InboundMessage(
            message_id=record.platform_message_id,
            event_type="message:snapshot_replay",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity(
                record.sender_user_id,
                nickname=record.sender_nickname,
                group_card=record.sender_group_card,
            ),
            text=record.content,
            bot_user_id=record.bot_user_id,
            group_id=record.group_id,
            segments=record.segments,
            received_at=record.occurred_at,
            mentioned_user_ids=record.mentioned_user_ids,
            reply_to_event_id=record.reply_to_event_id,
            conversation_id=scope["id"],
            person_id=record.author_person_id,
            presence_id=record.author_presence_id,
            space_id=scope["space_id"],
            source_event_id=record.id,
            legacy_conversation_key=scope["scope_key"],
        )
        turn = ConversationTurnSnapshot(
            scope["id"], scope["scope_key"], scope["generation"], record.id, 1
        )
        runtime = await chat._runtime_config.snapshot(
            user_id=record.sender_user_id, group_id=record.group_id
        )
        profile = UserProfileSnapshot(
            record.sender_user_id,
            ScopeType.GROUP,
            record.sender_nickname,
            record.group_id,
            record.sender_group_card,
        )
        prepare = {
            "inbound": inbound,
            "identity": identity,
            "profile": profile,
            "turn": turn,
            "content": record.content,
            "runtime": runtime,
        }
        context = await chat._context_assembler.assemble(**prepare)
        ordinary_inputs.append((prepare, runtime, inbound, original["read_scope"]))
        await capture(
            f"ordinary_actor_{index + 1}", context, runtime, inbound, original["read_scope"]
        )
    if ordinary_inputs:
        prepare, runtime, inbound, read_scope = ordinary_inputs[-1]
        context = await chat._context_assembler.assemble(**prepare)
        await capture("same_actor_repeat", context, runtime, inbound, read_scope)
        state.snapshot = lambda: []
        context = await chat._context_assembler.assemble(**prepare)
        await capture(
            "controlled_empty_material",
            replace(context, metadata_payload={}),
            runtime,
            inbound,
            read_scope,
        )
        state.snapshot = lambda: deepcopy(control["short_state"])

    # Prepare actual saved sources only; completed sources are never re-admitted or executed.
    if args.child == "current" and control["self"] is not None:
        saved = control["self"]
        source = json.loads(saved["source_json"])
        identity = ConversationScope.group(source["bot_user_id"], source["group_id"])
        turn = ConversationTurnSnapshot(
            source["conversation_id"],
            saved["scope_key"],
            saved["generation"],
            None,
            1,
            initiative_run_id=source["initiative_run_id"],
        )
        runtime = await chat._runtime_config.snapshot(group_id=source["group_id"])
        context = await chat._context_assembler.assemble(
            inbound=None,
            identity=identity,
            profile=None,
            turn=turn,
            content=source["instruction"],
            runtime=runtime,
        )
        await capture("saved_self_source", context, runtime, read_scope=source.get("read_scope"))
        with sqlite3.connect(target) as reader:
            reader.row_factory = sqlite3.Row
            scheduled = reader.execute(
                "SELECT r.id run_id,"
                "json_extract(v.script_json,'$.steps[0].arguments.instruction') instruction,"
                "v.authority_snapshot_json,cc.id conversation_id,"
                "cc.generation,la.scope_key FROM automation_runs r "
                "JOIN automation_versions v ON v.id=r.version_id "
                "JOIN canonical_conversations cc "
                "ON cc.id=json_extract(v.authority_snapshot_json,'$.canonical_conversation_id') "
                "JOIN conversation_legacy_aliases la ON la.id=cc.primary_alias_id "
                "WHERE json_array_length(v.script_json,'$.steps')=1 "
                "AND json_extract(v.script_json,'$.steps[0].call')='yuki.agent' "
                "ORDER BY r.id DESC LIMIT 1"
            ).fetchone()
        if scheduled:
            authority = json.loads(scheduled["authority_snapshot_json"])
            identity = ConversationScope.group(
                authority["bot_user_id"], authority["current_group_id"]
            )
            turn = ConversationTurnSnapshot(
                scheduled["conversation_id"],
                scheduled["scope_key"],
                scheduled["generation"],
                None,
                1,
                automation_run_id=scheduled["run_id"],
            )
            runtime = await chat._runtime_config.snapshot(group_id=authority["current_group_id"])
            context = await chat._context_assembler.assemble(
                inbound=None,
                identity=identity,
                profile=None,
                turn=turn,
                content=scheduled["instruction"],
                runtime=runtime,
            )
            await capture("saved_scheduled_source", context, runtime)

    write_jsonl(output / "chatrequests.jsonl", requests)
    write_json(output / "cases.json", cases)
    for protocol, values in bodies.items():
        write_jsonl(output / f"{protocol}.bodies.jsonl", values)
    with sqlite3.connect(target) as reader:
        reader.row_factory = sqlite3.Row
        final = database_inventory(reader)
    write_json(output / "public-history.json", final)
    write_json(
        output / "effects.json",
        {
            "model_calls": 0,
            "http_dispatches": 0,
            "qq_sends": 0,
            "source_snapshot_read_only": True,
            "business_execution_replayed": False,
        },
    )
    await database.close()


def compare_snapshot(output: Path) -> dict[str, Any]:
    old = json.loads((output / "old" / "cases.json").read_text(encoding="utf-8"))
    current = json.loads((output / "current" / "cases.json").read_text(encoding="utf-8"))
    old_by_name = {case["name"]: case for case in old}
    comparisons = []
    for case in current:
        previous = old_by_name.get(case["name"])
        if previous is None:
            continue
        comparisons.append(
            {
                "case": case["name"],
                "actor_and_read_scope_equal": (case["actor_id"], case["read_scope"])
                == (previous["actor_id"], previous["read_scope"]),
                "raw_history_equal": case["raw_history_sha256"] == previous["raw_history_sha256"],
                "current_event_rendering_equal": case["original_current_sha256"]
                == previous["original_current_sha256"],
                "old_dynamic_characters": previous["dynamic_characters"],
                "current_dynamic_characters": case["dynamic_characters"],
                "wire_bytes": {
                    protocol: {
                        "old": previous["body"][protocol]["bytes"],
                        "current": body["bytes"],
                        "difference": body["bytes"] - previous["body"][protocol]["bytes"],
                    }
                    for protocol, body in case["body"].items()
                },
            }
        )
    contracts = {}
    for protocol in ("gemini", "claude", "chat_completions", "responses"):
        values = {}
        for side in ("old", "current"):
            bodies = load_jsonl(output / side / f"{protocol}.bodies.jsonl")
            values[side] = [sha(fixed_wire(body, protocol)) for body in bodies]
        contracts[protocol] = {
            "current_requests": len(values["current"]),
            "old_requests": len(values["old"]),
            "within_current_system_tools_generation_equal": len(set(values["current"])) == 1,
            "within_old_system_tools_generation_equal": len(set(values["old"])) == 1,
            "between_arms_system_tools_generation_equal": len(
                set(values["current"] + values["old"])
            )
            == 1,
            "fixed_shape_sha256": values["current"][0],
        }
    control = json.loads((output / "controls.json").read_text(encoding="utf-8"))
    current_gemini = load_jsonl(output / "current" / "gemini.bodies.jsonl")[0]
    migration = json.loads((output / "current" / "migration.json").read_text(encoding="utf-8"))
    public = json.loads((output / "current" / "public-history.json").read_text(encoding="utf-8"))
    report = {
        "comparisons": comparisons,
        "entry_cases": [case["name"] for case in current],
        "fixed_http_contract": contracts,
        "migration": migration,
        "current_public_compiled_material_selections": public["compiled_material_selections"],
        "current_public_automatic_roots": public["public_automatic_roots"],
        "current_public_automatic_sources": public["automatic_sources"],
        "captured_gemini_fixed_http_fields_equal": fixed_wire(current_gemini, "gemini")
        == fixed_wire(control["prepared_body"], "gemini"),
        "gemini_export_provenance": {
            "request_source": control["request_source"],
            "request_to_wire": control["request_to_wire"],
            "first_body_sha256": {
                side: sha(load_jsonl(output / side / "gemini.bodies.jsonl")[0])
                for side in ("old", "current")
            },
            "file_sha256": {
                side: sha((output / side / "gemini.bodies.jsonl").read_bytes())
                for side in ("old", "current")
            },
        },
        "limits": [
            "Generated bodies use real serializers without transport; usage is unmeasured.",
            "Captured production system/tools/generation are shared experiment controls.",
            "effective_request reconstructs serializer input; it is not a recovered live Profile.",
            "Gemini fixed HTTP fields are verified against the strictly linked original prepared.",
            "Other protocols use generic serializer options, not a claimed original live Profile.",
            "Both arms share the current venv; recorded SP pin differences are not exercised.",
            "Snapshot state and capture state have separate timestamps retained in controls.json.",
            "Saved SELF/scheduled sources are composed, not admitted or executed again.",
            "Private journals/receipts are compared byte-for-byte across migration.",
            "QQ behavior, paid cache benefit and complete cost require separate validation.",
        ],
    }
    write_json(output / "snapshot-comparison.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contexts", type=Path)
    parser.add_argument("--proxy-contexts", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-contexts", type=Path)
    parser.add_argument("--child", choices=("old", "current"), help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.child:
        sys.path[:0] = [str(args.source_root / "src"), str(args.source_root)]
        asyncio.run(run_snapshot_arm(args))
        return
    if args.contexts is None:
        parser.error("--contexts is required")
    if args.snapshot and args.snapshot_contexts is None:
        parser.error("--snapshot-contexts is required with --snapshot")
    args.output.mkdir(parents=True, exist_ok=False)
    recorded = recorded_evidence(args.contexts.resolve(), args.output)
    if args.proxy_contexts:
        rows = load_jsonl(args.proxy_contexts)
        write_json(
            args.output / "proxy-provenance.json",
            {
                "source": str(args.proxy_contexts.resolve()),
                "sha256": sha(args.proxy_contexts.read_bytes()),
                "record_types": dict(Counter(row.get("record_type") for row in rows)),
                "manifest": rows[0],
                "limits": "This probe does not infer or rewrite upstream session identity.",
            },
        )
    comparison = None
    if args.snapshot:
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "src"))
        control = snapshot_controls(args.snapshot.resolve(), args.snapshot_contexts.resolve())
        write_json(args.output / "controls.json", control)
        freeze_sources(root, args.output)
        for name, source in (("old", "baseline-source"), ("current", "current-source")):
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--output",
                    str(args.output),
                    "--child",
                    name,
                    "--source-root",
                    str(args.output / source),
                ],
                cwd=args.output / source,
                check=True,
            )
        comparison = compare_snapshot(args.output)
    write_json(
        args.output / "summary.json",
        {
            "recorded": recorded,
            "snapshot": comparison,
            "paid_requests_added": 0,
            "qq_acceptance_measured": False,
        },
    )
    print(
        canonical(
            {
                "output": str(args.output),
                "recorded_contexts": recorded["contexts"],
                "snapshot_composed": comparison is not None,
                "paid_requests_added": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
