"""Yuki CLI for setup, diagnostics, personal clients, and plugin management."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from yuki import __version__
from yuki.acp_adapter.cli import add_acp_parser, run_acp_command
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import RuntimeConfigSnapshot
from yuki.admin_api.cli import add_admin_parser, admin_command
from yuki.admin_api.client import admin_client, mutate, plugin_mutation, read
from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules.model_runtime import ModelRuntimeModule
from yuki.character.repository import CharacterRepository
from yuki.client_api.identity import ClientIdentityService
from yuki.client_sdk.cli import add_client_parser, run_client_command
from yuki.config import Settings
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.deployment_setup import add_setup_parser, run_setup_command
from yuki.gateway.compatibility import provider_doctor_payload
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.snowluma import SNOWLUMA_PROVIDER_ID
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.http_environment import load_http_environment
from yuki.identity.person_identity import current_person_id
from yuki.identity.routing import PresenceRouter
from yuki.memory.dream.db_models import MemoryDreamRunModel
from yuki.memory.episode_index import EpisodeIndex
from yuki.memory.episode_titles import EpisodeTitleRejected, EpisodeTitles, set_episode_title
from yuki.memory.quality.audit import MemoryProductionQualityAudit
from yuki.memory.quality.hygiene import MemoryProvenanceHygiene
from yuki.memory.quality.task_scoped import (
    TaskScopedHygiene,
    TaskScopedPlan,
    TaskScopedScanRejected,
    automatic_fact_weekly_stats,
)
from yuki.memory.repository import MemoryJobRepository
from yuki.memory.resident import (
    ResidentMemory,
    ResidentOwner,
    ResidentSet,
    ResidentValidationError,
)
from yuki.memory.resident_selection import (
    ResidentOwnerLocked,
    ResidentQueue,
    ResidentSelection,
    ResidentSelectionOutcome,
    ResidentSelectionRejected,
    owner_label,
)
from yuki.model_runtime import (
    ModelInvocationRepository,
    ModelProfileCatalog,
    ModelTask,
    load_model_profile_catalog,
)
from yuki.model_runtime.executor import ModelExecutor
from yuki.model_runtime.legacy_migration import run_migration_command
from yuki.model_runtime.models import ModelStats
from yuki.model_runtime.profiles import model_profile_environment
from yuki.model_runtime.structured import StructuredTaskError
from yuki.persistence.database import Database
from yuki.persistence.models import MemoryFactModel
from yuki.plugin_host.manifest import load_manifest
from yuki.services.concurrency import ConcurrencyManager
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.testing.contract import run_plugin_contract_tests


def _init_database(settings: Settings) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


async def _initialize_character(settings: Settings, name: str) -> None:
    database = Database(settings.database_url)
    try:
        identity = ClientIdentityService(
            database,
            PresenceRouter(
                database, GatewayConnectionRegistry(providers=builtin_provider_catalog())
            ),
        )
        current = await CharacterRepository(identity, settings).initialize(name)
        print(json.dumps(asdict(current), ensure_ascii=False))
    finally:
        await database.close()


def _render_snowluma_config(settings: Settings, output: Path) -> None:
    """Merge Yuki's reverse WS client without replacing SnowLuma-owned settings."""

    target_url = os.getenv(
        "SNOWLUMA_REVERSE_WS_URL",
        "ws://bot:8080/onebot/v11/snowluma/ws",
    )
    if output.is_file():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("existing SnowLuma config is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("existing SnowLuma config root must be an object")
    else:
        payload = {}
    networks = payload.setdefault("networks", {})
    if not isinstance(networks, dict):
        raise ValueError("existing SnowLuma networks config must be an object")
    clients = networks.setdefault("wsClients", [])
    if not isinstance(clients, list):
        raise ValueError("existing SnowLuma wsClients config must be an array")
    managed = {
        "name": "yuki",
        "enabled": True,
        "url": target_url,
        "role": "Universal",
        "accessToken": settings.onebot_access_token,
        "messageFormat": "array",
        "reportSelfMessage": False,
        "reconnectIntervalMs": 30000,
    }
    merged: list[object] = []
    replaced = False
    for item in clients:
        if isinstance(item, dict) and item.get("name") == "yuki":
            if not replaced:
                merged.append(managed)
                replaced = True
            continue
        merged.append(item)
    if not replaced:
        merged.append(managed)
    networks["wsClients"] = merged
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _add_plugin_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    plugin = subparsers.add_parser(
        "plugin", help=f"管理本地可信 Plugin API {PLUGIN_API_VERSION} 插件"
    )
    commands = plugin.add_subparsers(dest="plugin_command", required=True)
    commands.add_parser("list")
    commands.add_parser("discover")
    for name in ("inspect", "permissions", "approve", "enable", "disable", "doctor"):
        parser = commands.add_parser(name)
        parser.add_argument("plugin_id")
        if name == "approve":
            parser.add_argument("--permission", action="append", default=[])
    validate = commands.add_parser("validate")
    validate.add_argument("path", type=Path)
    docs = commands.add_parser("docs")
    docs.add_argument("path", type=Path)
    test = commands.add_parser("test")
    test.add_argument("path", type=Path)
    configuration = commands.add_parser("configuration")
    configuration.add_argument("plugin_id")
    configuration.add_argument("--scope", choices=("global", "user", "group"), default="global")
    configuration.add_argument("--owner")
    configure = commands.add_parser("configure")
    configure.add_argument("plugin_id")
    configure.add_argument("file", type=Path, help="含 scope_type、owner_id 与 values 的 JSON 文件")
    configure.add_argument("--revision", type=int, required=True)
    configure.add_argument("--request-id")
    observe = commands.add_parser("observe")
    observe.add_argument("plugin_id")
    observe.add_argument("--cursor")
    observe.add_argument("--limit", type=int, default=10)
    retry = commands.add_parser("retry-outbox")
    retry.add_argument("id", type=int)
    retry.add_argument("--revision", type=int, required=True)
    retry.add_argument("--request-id")


def _add_diagnostics_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    model = subparsers.add_parser("model", help="检查模型档案、路由和调用统计")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("routes")
    model_commands.add_parser("profiles")
    model_commands.add_parser("stats")

    runtime = subparsers.add_parser("runtime", help="只读 Runtime / Capability / Memory 诊断")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_commands.add_parser("snapshot")
    runtime_commands.add_parser("memory-session")


def _add_memory_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    memory = subparsers.add_parser("memory", help="Memory 来源审计与显式治理")
    commands = memory.add_subparsers(dest="memory_command", required=True)
    audit = commands.add_parser("audit", help="只读、无内容的生产数据库检查")
    audit.add_argument("--database-url", required=True)
    stats = commands.add_parser("stats", help="只读、无正文的提取与召回统计")
    stats.add_argument("--database-url", required=True)
    stats.add_argument("--hours", type=float, default=24)
    hygiene = commands.add_parser("hygiene", help="指纹保护的显式来源治理")
    hygiene_commands = hygiene.add_subparsers(dest="hygiene_command", required=True)
    scan = hygiene_commands.add_parser("scan")
    scan.add_argument("--database-url", required=True)
    apply = hygiene_commands.add_parser("apply")
    apply.add_argument("fingerprint")
    apply.add_argument("--database-url", required=True)
    rebuild_fts = hygiene_commands.add_parser("rebuild-fts", help="在独立维护窗口全量重建 FTS 索引")
    rebuild_fts.add_argument("fingerprint")
    rebuild_fts.add_argument("--database-url", required=True)
    _add_task_scoped_parser(hygiene_commands)
    _add_resident_parser(commands)
    _add_episodes_parser(commands)


def _add_task_scoped_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    task_scoped = commands.add_parser(
        "task-scoped",
        help="存量当次任务指令：scan 只读出计划（只有 ID 与签名），所有者审阅后 apply，可 revert",
    )
    task_commands = task_scoped.add_subparsers(dest="task_scoped_command", required=True)
    scan = task_commands.add_parser(
        "scan", help="用 memory_dream 模型路由按提取规则分类，写出计划文件；不写库"
    )
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--limit", type=int, help="最多分类这么多条候选（按 fact ID 升序）")
    scan.add_argument("--database-url", help="默认使用当前配置的数据库")
    for name, text in (
        ("show", "列出计划里的 fact ID、主人与 kind"),
        ("apply", "全部事实仍 active 且签名一致才逐条失效（administrator_invalidated）"),
        ("revert", "恢复计划里仍以 administrator_invalidated 失效的事实"),
    ):
        parser = task_commands.add_parser(name, help=text)
        parser.add_argument("--plan", type=Path, required=True)
        parser.add_argument("--database-url", help="默认使用当前配置的数据库")
        if name == "show":
            parser.add_argument(
                "--content", action="store_true", help="同时打印正文；只在部署环境内使用"
            )


def _add_resident_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    resident = commands.add_parser(
        "resident", help="常驻记忆核心集：查看、渲染、检查、改写与挑选；不看运行时开关"
    )
    resident_commands = resident.add_subparsers(dest="resident_command", required=True)

    def owner_arguments(
        parser: argparse.ArgumentParser, *, allow_self: bool
    ) -> argparse.ArgumentParser:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--person", help="人物 ID；按合并后的根人物读写")
        group.add_argument("--space", help="群 space ID")
        if allow_self:
            group.add_argument("--self", dest="self_owner", action="store_true")
        parser.add_argument("--database-url", help="默认使用当前配置的数据库")
        return parser

    show = owner_arguments(
        resident_commands.add_parser("show", help="列出主人的明确事实 ID 与核心集"), allow_self=True
    )
    show.add_argument("--content", action="store_true", help="同时打印正文")
    render = resident_commands.add_parser("render", help="打印该会话此刻会渲染出的常驻段")
    render.add_argument("--conversation", required=True)
    render.add_argument("--database-url")
    check = resident_commands.add_parser(
        "check", help="对全部会话做两次只读渲染，输出渲染出的 fact ID 与不稳定的会话"
    )
    check.add_argument("--database-url")
    set_command = owner_arguments(
        resident_commands.add_parser("set", help="改写并锁定核心集（source=owner）"),
        allow_self=True,
    )
    set_command.add_argument("--facts", required=True, help="逗号分隔的有序 fact ID；空串清空")
    owner_arguments(
        resident_commands.add_parser("unlock", help="把核心集交还 Dream 挑选"), allow_self=False
    )
    select_command = owner_arguments(
        resident_commands.add_parser(
            "select", help="立即用 memory_dream 模型路由对一个主人跑一次挑选"
        ),
        allow_self=False,
    )
    select_command.add_argument("--dry-run", action="store_true", help="只打印结果，不写库")
    select_command.add_argument("--content", action="store_true", help="同时打印正文")
    status = resident_commands.add_parser("status", help="到期主人、最早未看候选与各主人修订号")
    status.add_argument("--database-url")


def _add_episodes_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    episodes = commands.add_parser(
        "episodes", help="经历目录：渲染、检查、标题回填与改写、水位状态；不看运行时开关"
    )
    episode_commands = episodes.add_subparsers(dest="episodes_command", required=True)
    render = episode_commands.add_parser("render", help="打印该会话此刻的经历目录与今日新经历")
    render.add_argument("--conversation", required=True)
    render.add_argument("--through-fact-id", type=int, help="用给定水位代替当前水位，供离线核对")
    check = episode_commands.add_parser(
        "check", help="对全部会话做两次只读渲染，输出列出的 fact ID 与不稳定的会话"
    )
    backfill = episode_commands.add_parser(
        "backfill-titles",
        help="为无标题的 active 经历补标题（recompose 取 focus，其余走 memory_dream）",
    )
    backfill.add_argument("--dry-run", action="store_true", help="只打印 fact ID 与标题，不写库")
    backfill.add_argument("--limit", type=int, help="最多处理这么多条，按 fact ID 升序")
    set_title = episode_commands.add_parser("set-title", help="改写一条经历的标题")
    set_title.add_argument("--fact", type=int, required=True)
    set_title.add_argument("--text", required=True)
    status = episode_commands.add_parser(
        "status", help="水位、运行中的 Dream、标题覆盖与各会话条数"
    )
    for parser in (render, check, backfill, set_title, status):
        parser.add_argument("--database-url", help="默认使用当前配置的数据库")


def _model_catalog(settings: Settings) -> ModelProfileCatalog:
    return load_model_profile_catalog(
        settings.model_profiles_file,
        environment=model_profile_environment(settings),
        web_mode=settings.web.mode,
        search_backend=settings.web_search_backend,
    )


async def _model_stats(settings: Settings) -> int:
    database = Database(settings.database_url)
    repository = ModelInvocationRepository(database)
    try:
        summary, tasks, _profiles = await repository.stats_summary()
        rows = {
            task.value: tasks.get(task.value, ModelStats()).model_dump(mode="json")
            for task in ModelTask
        }
        rows["all"] = summary.model_dump(mode="json")
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        await database.close()


async def _runtime_diagnostics(settings: Settings, args: argparse.Namespace) -> int:
    database = Database(settings.database_url)
    try:
        snapshot = await _runtime_snapshot(settings, database)
        command = str(args.runtime_command)
        if command == "snapshot":
            payload: dict[str, object] = asdict(snapshot)
        else:
            payload = {
                "memory": asdict(snapshot.memory),
                "conversation": asdict(snapshot.conversation),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        await database.close()


async def _runtime_snapshot(settings: Settings, database: Database) -> RuntimeConfigSnapshot:
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    return await runtime.snapshot()


async def _plugin_command(settings: Settings, args: argparse.Namespace) -> int:
    action = str(args.plugin_command)
    if action in {"validate", "test", "docs"}:
        path = Path(args.path)
        if action == "validate":
            manifest = load_manifest(path, yuki_version=__version__)
            print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2))
            return 0
        if action == "test":
            report = await run_plugin_contract_tests(path, yuki_version=__version__)
            print(report.model_dump_json(indent=2))
            return 0 if report.passed else 1
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        target = path / "plugin-api-reference.md"
        await asyncio.to_thread(
            target.write_text,
            (
                f"# Yuki Plugin API {PLUGIN_API_VERSION}\n\n"
                "由 `yuki plugin docs` 生成。完整手册位于 "
                "`docs/plugin-development/`。\n"
            ),
            encoding="utf-8",
        )
        print(target)
        return 0

    async with admin_client(settings) as client:
        if action == "list":
            result = await read(client, "plugins")
        elif action in {"inspect", "permissions"}:
            result = await read(client, f"plugins/{args.plugin_id}")
        elif action == "configuration":
            result = await read(
                client,
                f"plugins/{args.plugin_id}/configuration",
                **{"scope_type": args.scope, **({"owner_id": args.owner} if args.owner else {})},
            )
        elif action == "configure":
            result = await mutate(
                client,
                f"plugins/{args.plugin_id}/configuration",
                args.revision,
                request_id=args.request_id,
                **json.loads(args.file.read_text(encoding="utf-8")),
            )
        elif action == "observe":
            result = await read(
                client,
                f"plugins/{args.plugin_id}/observe",
                **{"limit": args.limit, **({"cursor": args.cursor} if args.cursor else {})},
            )
        elif action == "retry-outbox":
            result = await mutate(
                client,
                f"plugin-outbox/{args.id}/retry",
                args.revision,
                request_id=args.request_id,
            )
        else:
            identity = "yuki" if action == "discover" else str(args.plugin_id)
            result = await plugin_mutation(
                client,
                identity,
                action,
                permissions=list(args.permission) if action == "approve" else None,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


async def _memory_command(settings: Settings, args: argparse.Namespace) -> int:
    action = str(args.memory_command)
    if action == "audit":
        database = Database(str(args.database_url))
        try:
            audit_report = await MemoryProductionQualityAudit(database).run()
            print(audit_report.model_dump_json(indent=2))
            return int(audit_report.error_count > 0)
        finally:
            await database.close()
    if action == "stats":
        if not 0 < args.hours <= 24 * 90:
            raise ValueError("hours must be within (0, 2160]")
        database = Database(str(args.database_url))
        try:
            stats_report = {
                "generated_at": datetime.now(UTC).isoformat(),
                "window_hours": float(args.hours),
                "extraction_queue": await MemoryJobRepository(database).batch_health(
                    trigger_count=settings.memory_batch_trigger_count,
                    max_characters=settings.memory_batch_max_characters,
                    max_wait_seconds=settings.memory_batch_max_wait_seconds,
                ),
                "automatic_facts_by_week": await automatic_fact_weekly_stats(
                    database, since=datetime.now(UTC) - timedelta(hours=float(args.hours))
                ),
            }
            print(json.dumps(stats_report, ensure_ascii=False, indent=2))
            return 0
        finally:
            await database.close()
    if action == "hygiene" and args.hygiene_command == "task-scoped":
        return await _task_scoped_command(settings, args)
    if action == "hygiene":
        database = Database(str(args.database_url))
        try:
            hygiene = MemoryProvenanceHygiene(database)
            result = (
                await hygiene.scan()
                if args.hygiene_command == "scan"
                else (
                    await hygiene.rebuild_fts(str(args.fingerprint))
                    if args.hygiene_command == "rebuild-fts"
                    else await hygiene.apply(str(args.fingerprint))
                )
            )
            print(result.model_dump_json(indent=2))
            return 0
        finally:
            await database.close()
    if action == "resident":
        return await _resident_command(settings, args)
    if action == "episodes":
        return await _episodes_command(settings, args)
    return 1


async def _task_scoped_command(settings: Settings, args: argparse.Namespace) -> int:
    action = str(args.task_scoped_command)
    database = Database(str(args.database_url or settings.database_url))
    hygiene = TaskScopedHygiene(database)
    try:
        payload: dict[str, object]
        code = 0
        if action == "scan":
            try:
                async with _cli_models(settings, database) as (_active, models):
                    plan = await hygiene.scan(
                        models=models, concurrency=ConcurrencyManager(), limit=args.limit
                    )
            except (StructuredTaskError, TaskScopedScanRejected) as exc:
                print(json.dumps({"error": repr(exc)}, ensure_ascii=False))
                return 1
            plan.write(Path(args.output))
            payload = {"output": str(args.output), "planned": len(plan.facts)}
        else:
            plan = TaskScopedPlan.read(Path(args.plan))
            if action == "show":
                payload = {"facts": await hygiene.show(plan, content=bool(args.content))}
            elif action == "apply":
                applied = await hygiene.apply(plan)
                payload = applied.payload()
                code = int(bool(applied.rejected))
            else:
                payload = (await hygiene.revert(plan)).payload()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code
    finally:
        await database.close()


async def _resident_owner(database: Database, args: argparse.Namespace) -> ResidentOwner:
    if getattr(args, "self_owner", False):
        return ResidentOwner("self")
    if args.space is not None:
        return ResidentOwner("space", space_id=str(args.space))
    async with database.read_session() as session:
        return ResidentOwner("person", person_id=await current_person_id(session, args.person))


def _resident_set_payload(current: ResidentSet | None) -> dict[str, object] | None:
    if current is None:
        return None
    return {
        "fact_ids": list(current.fact_ids),
        "source": current.source,
        "revision": current.revision,
        "considered_through_fact_id": current.considered_through_fact_id,
        "selected_at": current.selected_at.isoformat() if current.selected_at else None,
    }


async def _resident_command(settings: Settings, args: argparse.Namespace) -> int:
    action = str(args.resident_command)
    database = Database(str(args.database_url or settings.database_url))
    resident = ResidentMemory(database)
    queue = ResidentQueue(database=database, resident=resident)
    try:
        payload: dict[str, object]
        if action == "render":
            rendered = await resident.render_conversation(str(args.conversation))
            print(rendered.text)
            payload = {
                "fact_ids": list(rendered.fact_ids),
                "omitted_explicit": rendered.omitted_explicit,
            }
        elif action == "check":
            payload = await _resident_check(database, resident)
        elif action == "status":
            pending = await queue.pending()
            sets = await queue.owner_sets()
            selected = [
                item.selected_at
                for item in sets
                if item.source == "dream" and item.selected_at is not None
            ]
            payload = {
                "due_owners": len(pending.owners),
                "oldest_pending_at": (
                    pending.oldest_pending_at.isoformat() if pending.oldest_pending_at else None
                ),
                "queue": [owner_label(item.owner) for item in pending.owners],
                "last_selected_at": max(selected).isoformat() if selected else None,
                "sets": [
                    {"owner": owner_label(item.owner), **(_resident_set_payload(item) or {})}
                    for item in sets
                ],
            }
        else:
            owner = await _resident_owner(database, args)
            payload = {"owner": owner_label(owner)}
            if action == "show":
                explicit = await queue.pinned_fact_ids(owner)
                current = await resident.load_set(owner)
                payload |= {
                    "explicit_fact_ids": list(explicit),
                    "core_set": _resident_set_payload(current),
                }
                if args.content:
                    shown = (*explicit, *(current.fact_ids if current else ()))
                    payload["content"] = await queue.fact_contents(shown)
            elif action == "set":
                facts = tuple(int(item) for item in str(args.facts).split(",") if item.strip())
                try:
                    written = await resident.write_set(owner, facts, source="owner")
                except ResidentValidationError as exc:
                    print(json.dumps({**payload, "error": str(exc)}, ensure_ascii=False))
                    return 1
                payload["core_set"] = _resident_set_payload(written)
            elif action == "unlock":
                payload["unlocked"] = await queue.unlock(owner)
            else:
                try:
                    outcome = await _resident_select(
                        settings, database, resident, owner, dry_run=bool(args.dry_run)
                    )
                except (
                    ResidentOwnerLocked,
                    ResidentSelectionRejected,
                    ResidentValidationError,
                    StructuredTaskError,
                ) as exc:
                    print(json.dumps({**payload, "error": repr(exc)}, ensure_ascii=False))
                    return 1
                payload |= {
                    "dry_run": bool(args.dry_run),
                    "previous": list(outcome.previous),
                    "selected": list(outcome.selected),
                    "added": list(outcome.added),
                    "removed": list(outcome.removed),
                    "pinned": list(outcome.pinned),
                    "offered_new": list(outcome.offered_new),
                    "considered_through_fact_id": outcome.considered_through_fact_id,
                    "model_calls": outcome.model_calls,
                    "core_set": _resident_set_payload(outcome.written),
                }
                if args.content:
                    payload["content"] = await queue.fact_contents(
                        (*outcome.previous, *outcome.offered_new)
                    )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await database.close()


async def _resident_check(database: Database, resident: ResidentMemory) -> dict[str, object]:
    """Batch-render every conversation twice; verification lives in an acceptance script."""

    conversations = await _conversation_rows(database)
    rendered: list[dict[str, object]] = []
    unstable: list[str] = []
    for conversation_id, kind, person_id, space_id in conversations:
        first = await resident.render_conversation(conversation_id)
        second = await resident.render_conversation(conversation_id)
        if first != second:
            unstable.append(conversation_id)
        if first.fact_ids:
            rendered.append(
                {
                    "conversation_id": conversation_id,
                    "kind": kind,
                    "person_id": person_id,
                    "space_id": space_id,
                    "fact_ids": list(first.fact_ids),
                    "omitted_explicit": first.omitted_explicit,
                }
            )
    return {
        "conversations": len(conversations),
        "with_resident_segment": len(rendered),
        "unstable": unstable,
        "rendered": rendered,
    }


async def _resident_select(
    settings: Settings,
    database: Database,
    resident: ResidentMemory,
    owner: ResidentOwner,
    *,
    dry_run: bool,
) -> ResidentSelectionOutcome:
    """Run one selection in this process with the bot's memory_dream model route.

    It needs no running bot and ignores ``memory.resident_enabled``.
    """

    async with _cli_models(settings, database) as (active, executor):
        return await ResidentSelection(
            settings=active,
            database=database,
            resident=resident,
            models=executor,
            concurrency=ConcurrencyManager(),
        ).select(owner, dry_run=dry_run)


@asynccontextmanager
async def _cli_models(
    settings: Settings, database: Database
) -> AsyncIterator[tuple[Settings, ModelExecutor]]:
    """The model runtime the bot builds at startup, assembled in this CLI process.

    Environment, model profile files and activated restart overrides apply; model
    invocations are recorded in the shared database. No running bot is needed.
    """

    runtime_config = RuntimeConfigService(settings=settings, database=database)
    await runtime_config.initialize()
    active = Settings(
        _env_file=None,
        **{**settings.model_dump(), **await runtime_config.startup_settings_updates()},
    )
    lifecycle = LifecycleRegistry()
    models = ModelRuntimeModule(
        active.model_runtime, database, lifecycle=lifecycle, web_settings=active.web
    ).build()
    await lifecycle.start()
    try:
        yield active, models.executor
    finally:
        await lifecycle.close()


async def _episodes_command(settings: Settings, args: argparse.Namespace) -> int:
    action = str(args.episodes_command)
    database = Database(str(args.database_url or settings.database_url))
    episodes = EpisodeIndex(database, timezone=settings.memory_dream_timezone)
    try:
        payload: dict[str, object]
        if action == "render":
            rendered = await episodes.render_conversation(
                str(args.conversation), through_fact_id=args.through_fact_id
            )
            print(rendered.index_text)
            print()
            print(rendered.recent_text)
            payload = {
                "index_fact_ids": list(rendered.index_fact_ids),
                "recent_fact_ids": list(rendered.recent_fact_ids),
                "through_fact_id": rendered.through_fact_id,
            }
        elif action == "check":
            payload = await _episodes_check(database, episodes)
        elif action == "status":
            payload = await _episodes_status(database, episodes)
        elif action == "set-title":
            try:
                await set_episode_title(database, int(args.fact), str(args.text))
            except EpisodeTitleRejected as exc:
                print(json.dumps({"fact_id": args.fact, "error": str(exc)}, ensure_ascii=False))
                return 1
            payload = {"fact_id": args.fact, "title": args.text}
        else:
            async with _cli_models(settings, database) as (active, executor):
                try:
                    outcome = await EpisodeTitles(
                        settings=active,
                        database=database,
                        models=executor,
                        concurrency=ConcurrencyManager(),
                    ).backfill(dry_run=bool(args.dry_run), limit=args.limit)
                except (EpisodeTitleRejected, StructuredTaskError) as exc:
                    print(json.dumps({"error": repr(exc)}, ensure_ascii=False))
                    return 1
            payload = {
                "dry_run": outcome.dry_run,
                "model_calls": outcome.model_calls,
                "items": [asdict(item) for item in outcome.items],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await database.close()


async def _conversation_rows(database: Database) -> tuple[Any, ...]:
    async with database.read_session() as session:
        return tuple(
            (
                await session.execute(
                    select(
                        CanonicalConversationModel.id,
                        CanonicalConversationModel.kind,
                        CanonicalConversationModel.person_id,
                        CanonicalConversationModel.space_id,
                    ).order_by(CanonicalConversationModel.id)
                )
            ).all()
        )


async def _episodes_check(database: Database, episodes: EpisodeIndex) -> dict[str, object]:
    """Render every conversation twice; verification lives in an acceptance script."""

    conversations = await _conversation_rows(database)
    rendered: list[dict[str, object]] = []
    unstable: list[str] = []
    through = 0
    for conversation_id, kind, person_id, space_id in conversations:
        first = await episodes.render_conversation(conversation_id)
        second = await episodes.render_conversation(conversation_id)
        through = first.through_fact_id
        if first != second:
            unstable.append(conversation_id)
        if first.index_fact_ids or first.recent_fact_ids:
            rendered.append(
                {
                    "conversation_id": conversation_id,
                    "kind": kind,
                    "person_id": person_id,
                    "space_id": space_id,
                    "index_fact_ids": list(first.index_fact_ids),
                    "recent_fact_ids": list(first.recent_fact_ids),
                }
            )
    return {
        "conversations": len(conversations),
        "through_fact_id": through,
        "with_episodes": len(rendered),
        "unstable": unstable,
        "rendered": rendered,
    }


async def _episodes_status(database: Database, episodes: EpisodeIndex) -> dict[str, object]:
    mark = await episodes.watermark()
    run = MemoryDreamRunModel
    fact = MemoryFactModel
    async with database.read_session() as session:
        active = await session.scalar(
            select(run).where(run.status.in_(("planned", "running"))).order_by(run.id.desc())
        )
        titled: dict[bool, int] = {
            bool(has_title): int(count)
            for has_title, count in await session.execute(
                select(fact.title.is_not(None), func.count())
                .where(fact.kind == "episode", fact.status == "active")
                .group_by(fact.title.is_not(None))
            )
        }
    conversations = []
    for conversation_id, *_identity in await _conversation_rows(database):
        rendered = await episodes.render_conversation(conversation_id)
        if rendered.index_fact_ids or rendered.recent_fact_ids:
            conversations.append(
                {
                    "conversation_id": conversation_id,
                    "index": len(rendered.index_fact_ids),
                    "recent": len(rendered.recent_fact_ids),
                }
            )
    return {
        "watermark": {
            "through_fact_id": mark.through_fact_id,
            "run_id": mark.run_public_id,
            "status": mark.status,
            "completed_at": mark.completed_at.isoformat() if mark.completed_at else None,
        },
        "active_run": None
        if active is None
        else {
            "run_id": active.public_id,
            "status": active.status,
            "snapshot_max_fact_id": active.snapshot_max_fact_id,
            "started_at": active.started_at.isoformat() if active.started_at else None,
        },
        "episodes_with_title": titled.get(True, 0),
        "episodes_without_title": titled.get(False, 0),
        "conversations": conversations,
    }


def main() -> None:
    """Run one explicit administrative subcommand."""

    parser = argparse.ArgumentParser(prog="yuki")
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init-db", help="运行 Alembic 数据库迁移")
    initialize.add_argument("--character-name", help="明确初始化当前角色名称；已有角色保持原样")
    render_snowluma = subparsers.add_parser(
        "render-snowluma-config",
        help="合并 SnowLuma OneBot 配置",
    )
    render_snowluma.add_argument("--output", type=Path, required=True)
    gateway = subparsers.add_parser("gateway", help="检查 QQ Gateway Provider 契约")
    gateway_commands = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_doctor = gateway_commands.add_parser(
        "doctor",
        help="输出只读、无敏感信息的 OneBot 核心契约",
    )
    gateway_doctor.add_argument(
        "--provider",
        choices=(SNOWLUMA_PROVIDER_ID,),
        required=True,
    )
    add_setup_parser(subparsers)
    _add_plugin_parser(subparsers)
    _add_diagnostics_parsers(subparsers)
    _add_memory_parser(subparsers)

    add_admin_parser(subparsers)

    add_client_parser(subparsers)

    add_acp_parser(subparsers)
    args = parser.parse_args()
    if args.command == "acp":
        raise SystemExit(run_acp_command(args))
    if args.command == "client":
        raise SystemExit(run_client_command(args))
    if args.command == "setup":
        raise SystemExit(run_setup_command(args))
    if args.command == "gateway":
        print(json.dumps(provider_doctor_payload(str(args.provider)), ensure_ascii=False, indent=2))
        return
    if (
        args.command == "admin"
        and args.admin_command == "model"
        and args.model_action == "migrate-legacy"
    ):
        raise SystemExit(asyncio.run(run_migration_command(args.revision)))
    load_http_environment()
    settings = Settings()
    if args.command == "init-db":
        _init_database(settings)
        if args.character_name is not None:
            asyncio.run(_initialize_character(settings, args.character_name))
    elif args.command == "render-snowluma-config":
        _render_snowluma_config(settings, args.output)
    elif args.command == "plugin":
        raise SystemExit(asyncio.run(_plugin_command(settings, args)))
    elif args.command == "admin":
        raise SystemExit(asyncio.run(admin_command(settings, args)))
    elif args.command == "model":
        if args.model_command == "stats":
            raise SystemExit(asyncio.run(_model_stats(settings)))
        catalog = _model_catalog(settings)
        if args.model_command == "routes":
            result = {
                task.value: {
                    "profile_id": route.profile_id,
                    "required_capabilities": sorted(
                        item.value for item in route.required_capabilities
                    ),
                }
                for task, route in catalog.routes.items()
            }
        else:
            result = {
                profile_id: profile.model_dump(mode="json")
                for profile_id, profile in catalog.profiles.items()
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "runtime":
        raise SystemExit(asyncio.run(_runtime_diagnostics(settings, args)))
    elif args.command == "memory":
        raise SystemExit(asyncio.run(_memory_command(settings, args)))


if __name__ == "__main__":
    main()
