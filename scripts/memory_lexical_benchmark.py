"""Local cost benchmark for search_memory's Chinese term cutting (记忆意图召回评估 §3).

Compares, on synthetic Zipf-distributed CJK text (55% short facts of 35 characters, 45%
episodes of 275 characters, close to production), at each given fact count:

* a resident 1/2/3-gram inverted index in Python (what keeping a Diana-style n-gram
  index in memory would cost): build time and traced memory;
* the old lexical SQL (trigram FTS for three or more characters, else LIKE in ID order);
* the current lexical SQL (trigram FTS plus one weighted LIKE scan, as in
  ``memory/fts.py``) and the pool-only scorer of ``memory/lexical.py``.

Usage: ``uv run python scripts/memory_lexical_benchmark.py 1600 5000 20000``. Prints
JSON; numbers are machine-dependent.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
import tracemalloc

from yuki.memory.lexical import LexicalDocument, lexical_terms, score_documents

rng = random.Random(11)
HANZI = [chr(code) for code in range(0x4E00, 0x4E00 + 2500)]
WEIGHTS = [1 / (rank + 1) ** 0.9 for rank in range(len(HANZI))]


def sentence(length: int) -> str:
    return "".join(rng.choices(HANZI, weights=WEIGHTS, k=length))


def corpus(n: int) -> list[str]:
    return [sentence(35 if rng.random() < 0.55 else 275) for _ in range(n)]


def ngram_index(texts: list[str]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    for doc_id, text in enumerate(texts):
        for n in (1, 2, 3):
            for i in range(len(text) - n + 1):
                index.setdefault(text[i : i + n], set()).add(doc_id)
    return index


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)


def bench(n: int) -> dict[str, object]:
    texts = corpus(n)
    tracemalloc.start()
    started = time.perf_counter()
    index = ngram_index(texts)
    build = time.perf_counter() - started
    resident, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del index

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE f (id INTEGER PRIMARY KEY, status TEXT, content TEXT, k TEXT, c TEXT,"
        " title TEXT, importance INT)"
    )
    db.execute("CREATE INDEX ix_status ON f(status, id)")
    db.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(content, k, c, content='f', content_rowid='id',"
        " tokenize='trigram')"
    )
    db.executemany(
        "INSERT INTO f VALUES (?, 'active', ?, 'some_key', 'cat', NULL, 3)",
        [(i + 1, text) for i, text in enumerate(texts)],
    )
    db.execute("INSERT INTO fts(fts) VALUES ('rebuild')")
    queries = [sentence(rng.choice((2, 3, 4, 6, 10))) for _ in range(200)]
    old_ms: list[float] = []
    new_ms: list[float] = []
    score_ms: list[float] = []
    for query in queries:
        started = time.perf_counter()
        if len(query) >= 3:
            trigrams = " OR ".join(f'"{query[i : i + 3]}"' for i in range(len(query) - 2))
            db.execute(
                "SELECT f.id FROM fts JOIN f ON f.id = fts.rowid WHERE fts MATCH ?"
                " AND f.status='active' ORDER BY bm25(fts, 1.0, 4.0, 2.0) LIMIT 51",
                (trigrams,),
            ).fetchall()
        else:
            db.execute(
                "SELECT id FROM f WHERE status='active' AND content LIKE ? ORDER BY id LIMIT 51",
                (f"%{query}%",),
            ).fetchall()
        old_ms.append((time.perf_counter() - started) * 1000)

        terms = lexical_terms(query, term_limit=12)
        started = time.perf_counter()
        rows: list[tuple[int]] = []
        if terms.index_terms:
            rows += db.execute(
                "SELECT f.id FROM fts JOIN f ON f.id = fts.rowid WHERE fts MATCH ?"
                " AND f.status='active' ORDER BY bm25(fts, 1.0, 4.0, 2.0) LIMIT 51",
                (terms.fts_expression,),
            ).fetchall()
        score = " + ".join(
            "CASE WHEN content LIKE ? OR k LIKE ? OR c LIKE ? OR title LIKE ?"
            f" THEN {len(term)} ELSE 0 END"
            for term in terms.scan_terms
        )
        rows += db.execute(
            f"SELECT id FROM (SELECT id, importance, ({score}) AS s FROM f"
            " WHERE status='active') WHERE s > 0 ORDER BY s DESC, importance DESC, id DESC"
            " LIMIT 51",
            [f"%{term}%" for term in terms.scan_terms for _ in range(4)],
        ).fetchall()
        new_ms.append((time.perf_counter() - started) * 1000)

        pool = sorted({row[0] for row in rows} | set(rng.sample(range(1, n + 1), 50)))
        documents = [LexicalDocument(i, "", "", "some_key cat", texts[i - 1]) for i in pool]
        started = time.perf_counter()
        score_documents(terms, documents)
        score_ms.append((time.perf_counter() - started) * 1000)
    return {
        "facts": n,
        "resident_ngram_index_build_s": round(build, 2),
        "resident_ngram_index_mb": round(resident / 2**20, 1),
        "old_sql_ms_p50_p95": [percentile(old_ms, 0.5), percentile(old_ms, 0.95)],
        "new_sql_ms_p50_p95": [percentile(new_ms, 0.5), percentile(new_ms, 0.95)],
        "pool_score_ms_p50_p95": [percentile(score_ms, 0.5), percentile(score_ms, 0.95)],
    }


if __name__ == "__main__":
    print(json.dumps([bench(int(n)) for n in sys.argv[1:]], ensure_ascii=False, indent=1))
