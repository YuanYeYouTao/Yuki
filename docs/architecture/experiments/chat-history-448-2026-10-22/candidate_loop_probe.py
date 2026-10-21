"""Counterexample to repeatedly materializing an unbounded FTS IN subquery.

This isolates current relevant columns/indexes on synthetic data; it is not a
second full-schema runtime measurement. Every event supplies all trigrams of
the phrase, but no exact phrase. Both complete N+1 loops must really exhaust it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from yuki.persistence.history_search import (
    compile_match,
    match_fields,
    namespace_terms,
    prepare_query,
    prepare_terms,
)
from yuki.persistence.history_search_index import CREATE_HISTORY_FTS


def probe(count, directory):
    path = Path(directory) / f"candidates-{count}.db"
    conversation, person = str(uuid4()), str(uuid4())
    content = "aba x bab"
    query = prepare_query("ababa", "phrase")
    assert match_fields(query, content) is None
    tokens = namespace_terms(
        conversation, prepare_terms(content), author_person_id=person, author_kind="person"
    )
    expression = compile_match(conversation, query, person_ids=(person,))
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            "CREATE TABLE chat_events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "canonical_conversation_id TEXT NOT NULL,author_person_id TEXT,"
            "occurred_at TEXT NOT NULL,content TEXT NOT NULL,audio_transcript TEXT NOT NULL,"
            "suppression_status TEXT,event_kind TEXT NOT NULL);"
            "CREATE INDEX ix_chat_events_canonical_conversation_id ON chat_events"
            "(canonical_conversation_id);"
            "CREATE INDEX ix_chat_events_conversation_author ON chat_events"
            "(canonical_conversation_id,author_person_id);"
            "CREATE INDEX ix_chat_events_conversation_occurred ON chat_events"
            "(canonical_conversation_id,occurred_at);"
        )
        connection.execute(CREATE_HISTORY_FTS)
        connection.executemany(
            "INSERT INTO chat_events VALUES(?,?,?,'2026-10-22',?,'','keeper','message')",
            ((index, conversation, person, content) for index in range(1, count + 1)),
        )
        connection.executemany(
            "INSERT INTO chat_history_fts(rowid,tokens) VALUES(?,?)",
            ((index, tokens) for index in range(1, count + 1)),
        )
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM chat_history_fts WHERE chat_history_fts MATCH ?", (expression,)
        ).fetchone() == (count,)
        results = {}
        for forced_index in (False, True):
            for use_fts in (False, True):
                base = "SELECT id,content,audio_transcript FROM chat_events "
                if forced_index:
                    base += "INDEXED BY ix_chat_events_conversation_author "
                base += (
                    "WHERE canonical_conversation_id=:conversation AND author_person_id=:person "
                    "AND occurred_at>=:start AND id>0 AND event_kind='message' "
                    "AND (suppression_status IS NULL OR suppression_status='keeper') "
                )
                parameters = {
                    "conversation": conversation,
                    "person": person,
                    "start": "2000-01-01",
                    "match": expression,
                }
                boundary = None
                pages, verified, matches, instructions = 0, 0, 0, 0
                plans = []

                def progress():
                    nonlocal instructions
                    instructions += 1000
                    return 0

                connection.set_progress_handler(progress, 1000)
                before = time.perf_counter()
                while matches <= 20:
                    statement = base
                    if boundary is not None:
                        statement += "AND id<:before "
                        parameters["before"] = boundary
                    if use_fts:
                        statement += (
                            "AND id IN (SELECT rowid FROM chat_history_fts "
                            "WHERE chat_history_fts MATCH :match) "
                        )
                    statement += "ORDER BY id DESC LIMIT 21"
                    if pages == 0:
                        plans.append(
                            list(connection.execute("EXPLAIN QUERY PLAN " + statement, parameters))
                        )
                    rows = connection.execute(statement, parameters).fetchall()
                    pages += 1
                    for _event_id, text, audio in rows:
                        verified += 1
                        if match_fields(query, text, audio) is not None:
                            matches += 1
                    if len(rows) < 21:
                        break
                    boundary = rows[-1][0]
                    if pages == 1:
                        deep = base + "AND id<:before "
                        if use_fts:
                            deep += (
                                "AND id IN (SELECT rowid FROM chat_history_fts "
                                "WHERE chat_history_fts MATCH :match) "
                            )
                        deep += "ORDER BY id DESC LIMIT 21"
                        plans.append(
                            list(
                                connection.execute(
                                    "EXPLAIN QUERY PLAN " + deep, {**parameters, "before": boundary}
                                )
                            )
                        )
                elapsed = (time.perf_counter() - before) * 1000
                connection.set_progress_handler(None, 0)
                assert matches == 0 and verified == count
                label = ("held_author_index" if forced_index else "unhinted") + (
                    ":repeated_fts_in" if use_fts else ":metadata"
                )
                results[label] = {
                    "events": count,
                    "page_limit": 21,
                    "pages_exhausted": pages,
                    "verified_rows": verified,
                    "matches": matches,
                    "elapsed_ms": elapsed,
                    "approx_vm_quantum_1000": instructions,
                    "first_and_deep_plans": plans,
                }
        return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="yuki448-candidate-loop-") as directory:
        result = {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "history_search_sha256": hashlib.sha256(
                Path("src/yuki/persistence/history_search.py").read_bytes()
            ).hexdigest(),
            "vm_excludes_fts_internal_work": True,
            "results": {str(count): probe(count, directory) for count in (5000, 20000)},
        }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
