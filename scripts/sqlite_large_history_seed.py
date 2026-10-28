"""Untimed synthetic ledger/FTS bulk preparation for the fixed local Main probe.

No production input, padding table, schema change, or benchmark timing change.
Use the exact same --history-rows in every old/new run; select it once, outside
formal measurement, rather than filling each version independently to a size.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID


def message_identity_fields(event_table) -> tuple[str, str, tuple[str, ...]]:
    """Select the 0155 or pre-0155 identity names for the paired checkout."""
    if "origin_key" in event_table.c:
        return "origin_key", "ingress_key", ("ingress_key", "native_message_id")
    return "platform_message_id", "platform_message_id", ("message_id",)


def history_templates() -> tuple[str, ...]:
    subjects = (
        "今天把消息接入和数据库提交的路径又读了一遍。平台消息号只用来去重，已经入账的事件继续传内部编号，后面的工具不能拿昵称重新认人。",
        "群里讨论的查询慢问题已经有执行计划了。会话和作者的复合索引能够缩小范围，检查时还要保留时间上界，不能把另一轮的内容算进当前结果。",
        "这次整理发布说明时补上了桌面客户端入口，也把迁移步骤和依赖版本写清楚了。README里的例子需要能复制运行，测试结果和实际部署分开记录。",
        "午后准备去图书馆看线性代数，晚上再做两道概率题。资料放在同一个目录，错题保留原过程，周末复习时比较为什么当时选了那条思路。",
        "图片发送要先找到原文件和对应消息的引用。已经成功发出去的图片只读原回执，连接短暂断开不能当作没有发送，也不能再造一个新请求。",
        "我把工具调用前后的日志按原调用编号整理好了。模型输出为空时记录上游返回，缓存字段没有提供就保留未知，不把缺失写成零或者算成没有命中。",
        "前台聊天开始后，后台整理应该主动让路。整理已经做出的部分可以留下，下次仍沿原任务和已保存结果继续，不必重新跑一遍模型来证明它还在。",
        "那篇文章的段落顺序调整好了，开头先说明实际问题，中间放一个能复现的例子。最后留着还没确认的细节，别把推测写成已经验证的结果。",
        "客户端的账号和长期人物资料是两回事。换了账号不会复制一套记忆，旧消息仍认原作者；要把两个人合并时，需要明确的操作来源和完整的归属关系。",
        "今晚做饭用了番茄、鸡蛋和一点青菜。明天如果时间够，先去超市补米，再顺路取快递；不用把没有确定的安排写成必须完成的清单。",
        "服务关闭时先停止新的任务，再等待已经开始的文件写入和数据库操作实际结束。只取消等待协程并不代表后台线程已经停下，连接也还不能归还给别人。",
        "测试里用固定模型回复和内存平台，只验证调度、事件、工具及持久回执之间的关系。这里的耗时包含本地数据库工作，不代表真实网络模型的响应速度。",
        "搜索结果保留原始标题、链接和片段，引用要能回到对应来源。网页抓取失败时保留那个失败，不自动换一家服务或者悄悄发起第二次付费查询。",
        "工作区里已经把旧文档归档，当前说明直接按实际代码改。没人消费的中间状态和重复校验可以删掉，真实权限、来源及发送回执继续放在正确的边界上。",
        "下周读书会准备介绍一个小型并发例子：两个任务先各自准备输入，再顺序提交。讨论重点是哪些事实要一起变化，以及失败后还能确定什么已经发生。",
        "代码审查的备注改得短了一些。先说触发条件和实际后果，再给文件位置与复现方式；普通失败不需要额外包装成一套新的恢复框架。",
    )
    result = []
    for variant in range(128):
        lines = [
            f"合成历史讨论 / synthetic conversation note {variant:03d}",
            f"主题轮次 {variant % 23:02d}；准备资料日期 2026-09-{1 + variant % 28:02d}。",
        ]
        for offset in range(12 + variant % 9):
            lines.append(subjects[(variant + offset * 5) % len(subjects)])
        lines.extend(
            (
                "```text\nrequest=historical-discussion stage=review "
                f"case={variant:03d}\nstatus=recorded source=synthetic-local-only\n```",
                "Follow-up: retain the original event, inspect the actual result, and continue "
                "with the same request when it is still authorized. "
                "This is generated conversation material, not a production transcript.",
            )
        )
        result.append("\n\n".join(lines))
    return tuple(result)


async def prepare_large_history(database, environment, rows: int, checkout: Path) -> dict:
    """Import bounded chunks through the existing writer; never one await per event."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import func, insert, select, text

    from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
    from yuki.conversation.hydrate import bump_canonical_generation
    from yuki.persistence.history_search import namespace_terms, prepare_terms
    from yuki.persistence.models import ChatEventModel

    event_identity, receipt_identity, _ = message_identity_fields(ChatEventModel.__table__)
    if rows <= 0:
        raise ValueError("--history-rows must be positive when the history seed is requested")
    started = time.perf_counter()
    read = database.read_session if hasattr(database, "read_session") else database.sessions
    async with read() as snapshot:
        sample = dict(
            (
                await snapshot.execute(
                    select(ChatEventModel.__table__)
                    .where(
                        ChatEventModel.canonical_conversation_id
                        == environment.context.conversation_id,
                        ChatEventModel.direction == "inbound",
                        ChatEventModel.author_kind == "person",
                    )
                    .order_by(ChatEventModel.id)
                    .limit(1)
                )
            )
            .mappings()
            .one()
        )
        receipt = dict(
            (
                await snapshot.execute(
                    select(CanonicalEventReceiptModel.__table__).where(
                        CanonicalEventReceiptModel.canonical_event_id
                        == sample["canonical_event_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        first = int(await snapshot.scalar(select(func.max(ChatEventModel.id))) or 0) + 1

    # The fixed source body is genuine structured text, not zero/random padding.
    # Repeated templates are explicit synthetic corpus vocabulary, not a claim
    # that production text/cardinality has the same distribution.
    bodies = history_templates()
    segments = tuple(
        json.dumps([{"type": "text", "data": {"text": body}}], ensure_ascii=False)
        for body in bodies
    )
    fts_tokens = tuple(
        namespace_terms(
            sample["canonical_conversation_id"],
            prepare_terms(body),
            author_person_id=sample["author_person_id"],
            author_kind=sample["author_kind"],
        )
        for body in bodies
    )
    observed = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    occurred = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    batch_size = 512
    for start in range(0, rows, batch_size):
        events, receipts, terms = [], [], []
        for ordinal in range(start, min(rows, start + batch_size)):
            variant = ordinal % len(bodies)
            event_id = first + ordinal
            identity = str(UUID(int=0x6C617267656800000000000000000000 + ordinal, version=4))
            platform_id = f"synthetic-history-{ordinal:09d}"
            value = {
                **sample,
                "id": event_id,
                event_identity: platform_id,
                "canonical_event_id": identity,
                "content": bodies[variant],
                "segments_json": segments[variant],
                "occurred_at": occurred + timedelta(seconds=ordinal),
                "observed_at": observed,
                "visual_summary": "",
                "audio_transcript": "",
                "utterance_fingerprint": None,
                "reply_to_message_id": (
                    f"synthetic-history-{ordinal - 1:09d}" if ordinal and ordinal % 8 == 0 else None
                ),
                "reply_to_event_id": event_id - 1 if ordinal and ordinal % 8 == 0 else None,
                "caused_by_event_id": None,
            }
            if event_identity == "origin_key":
                # Bulk ledger rows have no actual platform-native receipt.
                value["native_message_id"] = None
            claim = {key: value for key, value in receipt.items() if key != "id"}
            claim.update(
                {receipt_identity: platform_id},
                canonical_event_id=identity,
                created_at=observed,
                observed_at=observed,
            )
            events.append(value)
            receipts.append(claim)
            terms.append({"id": event_id, "tokens": fts_tokens[variant]})
        async with database.immediate_session() as writer:
            await writer.execute(insert(CanonicalEventReceiptModel.__table__), receipts)
            await writer.execute(insert(ChatEventModel.__table__), events)
            await writer.execute(
                text("INSERT INTO chat_history_fts(rowid,tokens) VALUES(:id,:tokens)"), terms
            )

    # A real /ai new generation fence retains the permanent ledger/FTS while
    # leaving the active Main window at the original small workload. SELF's
    # first scan below the hook uses its original MAX(id) deployment baseline.
    last = first + rows - 1
    async with database.immediate_session() as writer:
        generation = await bump_canonical_generation(
            writer, sample["canonical_conversation_id"], event_id=last
        )

    # Same untimed checkpoint and connection warm-up for both versions.
    # Keep foreign keys, triggers, all actual indexes, WAL and durability settings.
    async with database.engine.connect() as connection:
        checkpoint = tuple(
            (await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")).one()
        )
        await connection.exec_driver_sql("SELECT 1")
        page_size = int((await connection.exec_driver_sql("PRAGMA page_size")).scalar_one())
        page_count = int((await connection.exec_driver_sql("PRAGMA page_count")).scalar_one())
        freelist = int((await connection.exec_driver_sql("PRAGMA freelist_count")).scalar_one())
        foreign_keys = int((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
        schema = (
            await connection.exec_driver_sql(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        ).fetchall()
        event_count = int(
            (
                await connection.exec_driver_sql(
                    "SELECT count(*) FROM chat_events WHERE id BETWEEN ? AND ?", (first, last)
                )
            ).scalar_one()
        )
        fts_count = int(
            (
                await connection.exec_driver_sql(
                    "SELECT count(*) FROM chat_history_fts WHERE rowid BETWEEN ? AND ?",
                    (first, last),
                )
            ).scalar_one()
        )
        receipt_count = int(
            (
                await connection.exec_driver_sql(
                    "SELECT count(*) FROM canonical_event_receipts "
                    f"WHERE {receipt_identity} LIKE 'synthetic-history-%'"
                )
            ).scalar_one()
        )
        actual_heads = (
            list(
                (
                    await connection.exec_driver_sql("SELECT version_num FROM alembic_version")
                ).scalars()
            )
            if any(row[1] == "alembic_version" for row in schema)
            else []
        )
        assert event_count == fts_count == receipt_count == rows
        assert foreign_keys == 1 and checkpoint[0] == 0
        # Exact original namespace/person tokens from the actual product helper.
        witness = fts_tokens[0].split()[0]
        indexed = await connection.exec_driver_sql(
            "SELECT rowid FROM chat_history_fts WHERE chat_history_fts MATCH ? AND rowid=?",
            (f'"{witness}"', first),
        )
        assert indexed.scalar_one() == first

    config = Config(str(checkout / "alembic.ini"))
    config.set_main_option("script_location", str(checkout / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    path = Path(database.engine.url.database)
    helper_bytes = await asyncio.to_thread(Path(__file__).read_bytes)
    database_stat = await asyncio.to_thread(path.stat)
    wal_stat = await asyncio.to_thread(Path(str(path) + "-wal").stat)
    schema_rows = [tuple(row) for row in schema]
    return {
        "requested_rows": rows,
        "events": event_count,
        "history_fts_rows": fts_count,
        "canonical_transport_receipts": receipt_count,
        "first_event_id": first,
        "last_event_id": last,
        "conversation_count": 1,
        "author_count": 1,
        "body_templates": len(bodies),
        "mean_content_utf8_bytes": statistics.mean(len(body.encode("utf-8")) for body in bodies),
        "mean_segments_utf8_bytes": statistics.mean(
            len(value.encode("utf-8")) for value in segments
        ),
        "corpus_sha256": hashlib.sha256("\n".join(bodies).encode("utf-8")).hexdigest(),
        "helper_sha256": hashlib.sha256(helper_bytes).hexdigest(),
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "allocated_database_bytes": page_count * page_size,
        "database_file_bytes": database_stat.st_size,
        "wal_file_bytes": wal_stat.st_size,
        "checkpoint": checkpoint,
        "foreign_keys": foreign_keys,
        "schema_source_heads": heads,
        "database_alembic_version": actual_heads,
        "schema_creation": (
            "existing benchmark Database.create_schema metadata fixture, not a deployed migration"
        ),
        "schema_sql_sha256": hashlib.sha256(json.dumps(schema_rows).encode()).hexdigest(),
        "chat_event_indexes": [
            row[1] for row in schema if row[0] == "index" and row[2] == "chat_events"
        ],
        "generation_after_original_reset": generation,
        "seed_seconds_excluded_from_timing": time.perf_counter() - started,
        "batch_size": batch_size,
        "size_semantics": (
            "fixed row count, measured actual pages/bytes; no per-version size-filling loop"
        ),
        "shape_limit": (
            "one real conversation/author; synthetic repeated vocabulary, "
            "not production table/content distribution"
        ),
    }
