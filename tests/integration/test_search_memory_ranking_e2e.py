"""E2E: search_memory ranking on constructed Chinese queries (记忆意图召回评估 §4).

Contract: docs/architecture/memory-v2-retrieval.md "查询与排序" / "词法打分". Every case
runs a real SQLite database with facts written through the real service, a real Main turn
whose scripted model calls ``search_memory`` (the tool's real authorization, query plane,
FTS, scan and Vec1 paths), and a deterministic embedding provider whose vectors fix each
fact's cosine to the query. Each case writes the query, every fact's cosine and the
returned order to ``test-results/memory-recall/<case>.json``. All text is invented.

Failure cases, written before the implementation:
  S1 a fact holding the exact topic word ranks below facts that are only semantically
     closer (the old cosine band), so it misses the four returned results.
  S2 a fact of the person named in the query whose content lacks the name misses the top
     four; another person's fact matching only the common word ranks above it.
  S3 with a two-character word in the query, a fact matching only single characters
     ("的", "猫") still counts as a lexical candidate and outranks the named person's fact.
  S4 a strongly semantic fact without lexical overlap drops out of the top four when
     three facts match one common word.
  S5 a fact whose content equals the query is not first.
  S6 the search issues more statements for a larger fact table, or a statement scans
     memory_facts, chat_events or memory_evidence.
  S7 a mixed-script word ("B站": one Latin letter joined to a CJK character) is cut into
     two single characters, so a fact holding the word ranks below facts that only hold
     "b" (in their key) and "站" apart.
  S8 a group whose name holds the query word ("咖啡同好群") puts every person_group fact
     of that group into the lexical window ahead of the person fact that holds the word,
     which then misses the top four: the group name counts as the subject name of facts
     about its members.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from tests.integration.test_memory_profiles_scale_e2e import _business, recorded
from tests.support.memory_resident_scene import ResidentScene, normalize

from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.enums import MemoryContextMode, MemoryRetrievalMode
from yuki.memory.models import MemoryQueryIntent
from yuki.memory.read_scope import MemoryReadScopeResolver
from yuki.persistence.models import MemoryFactModel

RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-recall"
NAME = "北川"  # B's display name in these cases
QUERY_VECTOR = (1.0, 0.0, 0.0, 0.0)
LARGE = ("memory_facts", "chat_events", "memory_evidence")


def _vector(cosine: float) -> tuple[float, float, float, float]:
    return (cosine, math.sqrt(1 - cosine * cosine), 0.0, 0.0)


@pytest.fixture
async def scene(database, tmp_path):
    built = await ResidentScene(database, tmp_path).build()
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == built.persons["B"])
            .values(display_name=NAME)
        )
    return built


async def _semantic(scene: ResidentScene, cosines: dict[int, float], queries: tuple[str, ...]):
    """Store one vector per fact and point the tool's retriever at a fake provider."""
    from yuki.memory.embedding.codec import Float32VectorCodec
    from yuki.memory.embedding.fake import FakeEmbeddingProvider
    from yuki.memory.embedding.models import EmbeddingVector
    from yuki.memory.embedding.repository import MemoryEmbeddingRepository
    from yuki.memory.embedding.semantic import MemorySemanticIndex
    from yuki.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder

    provider = FakeEmbeddingProvider(
        dimensions=4, vectors={query: QUERY_VECTOR for query in queries}
    )
    repository = MemoryEmbeddingRepository(scene.database)
    profile = await repository.ensure_profile(provider.profile)
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=1000)
    codec = Float32VectorCodec()
    async with scene.database.immediate_session() as session:
        facts = (
            await session.scalars(select(MemoryFactModel).where(MemoryFactModel.id.in_(cosines)))
        ).all()
        await MemoryEmbeddingRepository.store_vectors(
            session,
            profile_id=profile.id,
            records=[
                dict(
                    fact_id=fact.id,
                    profile_id=profile.id,
                    content_hash=documents.content_hash_fields(
                        kind=fact.kind,
                        category=fact.category,
                        memory_key=fact.memory_key,
                        content=fact.content,
                    ),
                    vector_blob=codec.encode(
                        EmbeddingVector(values=_vector(cosines[fact.id]), dimensions=4)
                    ),
                    created_at=fact.created_at,
                    updated_at=fact.created_at,
                )
                for fact in facts
            ],
        )
    retriever = scene.chat._tools._memory_context.retriever
    retriever.configure_semantic(
        semantic_index=MemorySemanticIndex(repository, documents=documents),
        provider=provider,
        profile=profile,
        queries=EmbeddingQueryBuilder(max_characters=1000),
    )
    return retriever


async def _search(scene: ResidentScene, query: str) -> list[int]:
    replies, _ = await scene.tool_turn(
        "A", f"帮我想想 {query}", [("search_memory", {"query": query})]
    )
    data = replies[0]
    assert "memories" in data, data
    return [item["fact_id"] for item in data["memories"]]


def _write(case: str, scene: ResidentScene, payload: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{case}.json").write_text(
        normalize(payload, scene.labels()) + "\n", encoding="utf-8"
    )


async def _facts(scene: ResidentScene, rows: list[tuple[str, str, str, float, int]]):
    """(name, owner, content, cosine, importance) -> name -> fact ID, ID -> cosine."""
    g1 = await scene.event("space:G", "B", "群里随便聊聊", key="g-ranking")
    ids: dict[str, int] = {}
    cosines: dict[int, float] = {}
    for name, owner, content, cosine, importance in rows:
        ids[name] = await scene.fact(
            name, owner, content, evidence=(g1,) if owner != "A" else (), importance=importance
        )
        cosines[ids[name]] = cosine
    return ids, cosines


def _record(query: str, ids: dict[str, int], cosines: dict[int, float], order: list[int]):
    names = {value: key for key, value in ids.items()}
    return {
        "query": query,
        "cosine": {name: cosines[fact_id] for name, fact_id in ids.items()},
        "order": [names.get(fact_id, fact_id) for fact_id in order],
    }


# ---------------------------------------------------------------------------------- S1
async def test_s1_exact_topic_word_beats_semantically_closer_facts(scene):
    ids, cosines = await _facts(
        scene,
        [
            ("zelda", "B", "最近在玩塞尔达", 0.30, 3),
            ("game", "G", "周末一起打游戏机", 0.90, 3),
            ("console", "B", "想换一台新主机", 0.85, 3),
            ("handheld", "A", "通勤路上玩掌机", 0.80, 3),
            ("arcade", "G", "群里约去电玩城", 0.75, 3),
            ("rpg", "B", "喜欢角色扮演游戏", 0.70, 3),
        ],
    )
    query = "塞尔达"
    await _semantic(scene, cosines, (query,))
    order = await _search(scene, query)

    assert order[0] == ids["zelda"], order
    _write("S1-topic-word", scene, _record(query, ids, cosines, order))


# ---------------------------------------------------------------------------------- S2
async def test_s2_named_person_fact_without_the_name_reaches_the_top_four(scene):
    ids, cosines = await _facts(
        scene,
        [
            ("coffee_b", "B", "每天早上喝咖啡", 0.30, 3),
            ("coffee_a", "A", "喜欢喝咖啡", 0.50, 3),
            ("tea", "A", "下午常喝奶茶", 0.90, 3),
            ("drink", "G", "群里聚会爱点饮料", 0.85, 3),
            ("juice", "A", "早餐配橙汁", 0.80, 3),
            ("water", "G", "大家都说要多喝水", 0.75, 3),
        ],
    )
    query = f"{NAME} 咖啡"
    await _semantic(scene, cosines, (query,))
    order = await _search(scene, query)

    assert ids["coffee_b"] in order[:4], order
    assert order.index(ids["coffee_b"]) < order.index(ids["tea"]), order
    _write("S2-named-person", scene, _record(query, ids, cosines, order))


# ---------------------------------------------------------------------------------- S3
async def test_s3_single_character_matches_do_not_count_beside_a_word(scene):
    ids, cosines = await _facts(
        scene,
        [
            ("noise_cat", "G", "楼下的猫粮的价格涨了", 0.50, 5),
            ("noise_de", "G", "我的手机的壳是蓝的", 0.50, 5),
            ("cat_b", "B", "养了一只橘猫", 0.50, 3),
            ("coffee_b", "B", "每天早上喝咖啡", 0.20, 3),
        ],
    )
    query = f"{NAME} 的 猫"
    await _semantic(scene, cosines, (query,))
    order = await _search(scene, query)

    assert order[0] == ids["cat_b"], order
    _write("S3-single-character", scene, _record(query, ids, cosines, order))


# ------------------------------------------------------------------------------ S4 S5
async def test_s4_s5_semantic_hit_stays_and_exact_match_first(scene):
    ids, cosines = await _facts(
        scene,
        [
            ("hike", "B", "喜欢爬山徒步", 0.95, 3),
            ("overtime", "B", "周末要加班", 0.20, 3),
            ("sleep", "A", "周末睡懒觉", 0.20, 3),
            ("clean", "G", "周末大扫除", 0.20, 3),
            ("movie", "B", "爱看科幻电影", 0.60, 3),
            ("exact", "B", "爱吃火锅", 0.10, 2),
        ],
    )
    queries = ("周末去哪", "爱吃火锅")
    await _semantic(scene, cosines, queries)
    weekend = await _search(scene, queries[0])
    exact = await _search(scene, queries[1])

    assert ids["hike"] in weekend[:4], weekend
    assert exact[0] == ids["exact"], exact
    _write(
        "S4-S5-ranking",
        scene,
        {
            "semantic_hit_with_common_word": _record(queries[0], ids, cosines, weekend),
            "exact_content": _record(queries[1], ids, cosines, exact),
        },
    )


# ---------------------------------------------------------------------------------- S6
def _plans(path: str, statements: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    from yuki.persistence.vec1 import extension_path

    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    connection.load_extension(str(extension_path()))
    plans = []
    try:
        for sql, params in statements:
            details = [
                row[3]
                for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", params or ()).fetchall()
            ]
            plans.append(
                {
                    "sql": " ".join(sql.split())[:160],
                    "plan": details,
                    "large_scans": [detail for detail in details if _large_scan(detail)],
                }
            )
    finally:
        connection.close()
    return plans


def _large_scan(detail: str) -> bool:
    """``SCAN <table or alias>`` of a table that grows with history (not an index search)."""
    words = detail.split()
    if len(words) < 2 or words[0] != "SCAN" or "VIRTUAL" in words:
        return False
    table = words[1].rstrip("_0123456789")
    return table in LARGE or table == "mf"


async def _statements(scene: ResidentScene, query: str) -> list[tuple[str, Any]]:
    context = scene.chat._tools._memory_context
    scope = await MemoryReadScopeResolver(scene.database).authorized(
        scene.persons["A"], private_scene=True
    )
    runtime = await scene.runtime()
    with recorded(scene.database) as statements:
        await context.search_authorized(
            text=query,
            mode=MemoryRetrievalMode.RELEVANT,
            scope=scope,
            runtime=runtime,
            limit=4,
            intent=MemoryQueryIntent(mode=MemoryContextMode.HYBRID),
        )
    return _business(statements)


async def test_s6_statement_count_constant_and_no_large_scan(scene):
    rows = [
        (f"small_{index}", "B" if index % 2 else "G", f"喜欢第{index}种咖啡", 0.5, 3)
        for index in range(6)
    ]
    _, cosines = await _facts(scene, rows)
    query = f"{NAME} 咖啡"
    await _semantic(scene, cosines, (query,))
    small = await _statements(scene, query)
    more = {}
    for index in range(120):
        fact_id = await scene.fact(
            f"large_{index}", "B" if index % 2 else "G", f"第{index}条：周末喝咖啡和奶茶"
        )
        more[fact_id] = 0.4
    await _semantic(scene, more, (query,))
    large = await _statements(scene, query)
    path = scene.database.url.split("///", 1)[1]
    plans = _plans(path, large)

    assert len(small) == len(large), (
        [" ".join(sql.split())[:90] for sql, _ in small],
        [" ".join(sql.split())[:90] for sql, _ in large],
    )
    assert not [plan for plan in plans if plan["large_scans"]], plans
    _write(
        "S6-statements",
        scene,
        {
            "statements": {"six_facts": len(small), "126_facts": len(large)},
            "plans": plans,
        },
    )


# ---------------------------------------------------------------------------------- S7
async def test_s7_mixed_script_word_stays_one_term(scene):
    ids, cosines = await _facts(
        scene,
        [
            ("bilibili", "B", "常在B站看番", 0.30, 3),
            ("bus", "A", "每天坐公交到站", 0.90, 3),
            ("blog", "G", "群里有人做了个人网站", 0.85, 3),
            ("bike", "A", "骑车去地铁站", 0.80, 3),
            ("bakery", "G", "车站旁边新开了面包店", 0.75, 3),
        ],
    )
    query = "B站"
    await _semantic(scene, cosines, (query,))
    order = await _search(scene, query)

    assert order[0] == ids["bilibili"], order
    _write("S7-mixed-script", scene, _record(query, ids, cosines, order))


# ---------------------------------------------------------------------------------- S8
async def test_s8_group_name_is_not_the_subject_name_of_member_facts(scene):
    from yuki.identity.db_models import CanonicalSpaceModel

    async with scene.database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == scene.spaces["G"])
            .values(name="咖啡同好群")
        )
    ids, cosines = await _facts(scene, [("coffee_b", "B", "每天早上喝咖啡", 0.10, 3)])
    for index in range(55):
        fact_id = await scene.fact(
            f"member_{index}", "B@G", f"群里第{index}次聊到周末安排", importance=5
        )
        cosines[fact_id] = 0.5
    query = "咖啡"
    await _semantic(scene, cosines, (query,))
    order = await _search(scene, query)

    assert ids["coffee_b"] in order[:4], order
    _write("S8-group-name", scene, _record(query, ids, cosines, order))
