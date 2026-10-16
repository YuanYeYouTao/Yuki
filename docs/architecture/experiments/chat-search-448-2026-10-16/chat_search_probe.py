"""Synthetic probes for the #448 chat-search taskbook.

Pure standard library + the Python sqlite3 module.  No Yuki code, no real chat
data, no network.  Every random choice is seeded, so a rerun on the same Python
and SQLite version reproduces the counts; timings vary with the machine.

Usage:
    python -I chat_search_probe.py --workdir /tmp/x --messages 200000 --out results.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sqlite3
import statistics
import time
import unicodedata
from collections import Counter
from datetime import UTC
from pathlib import Path

# ---------------------------------------------------------------- normalizer
# A deliberately tiny Traditional->Simplified sample.  The taskbook's real map
# comes from OpenCC TSCharacters; completeness does not depend on its size
# because queries and text go through the same function.
T2S = str.maketrans(
    {
        "報": "报",
        "銷": "销",
        "電": "电",
        "後": "后",
        "預": "预",
        "買": "买",
        "餐": "餐",
        "廳": "厅",
        "價": "价",
        "來": "来",
        "說": "说",
        "這": "这",
    }
)


def _fold(segment: str) -> str:
    text = unicodedata.normalize("NFKC", segment)
    text = unicodedata.normalize("NFKC", text.casefold())
    text = text.translate(T2S)
    return "".join(ch for ch in text if not ch.isspace())


def normalize(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return folded text and, per output char, the original [start, end) span.

    The original text is cut greedily into minimal units u such that folding
    u+next equals fold(u)+fold(next); each unit's output chars map to the
    whole unit.  This keeps NFKC compositions (combining marks, half-width
    voiced marks, conjoining jamo) inside one unit.
    """

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        end = index + 1
        while end < len(text) and _fold(text[index : end + 1]) != _fold(text[index:end]) + _fold(
            text[end]
        ):
            end += 1
        folded = _fold(text[index:end])
        for ch in folded:
            out.append(ch)
            spans.append((index, end))
        index = end
    del start
    return "".join(out), spans


def highlight(original: str, needle: str) -> list[tuple[int, int]]:
    folded, spans = normalize(original)
    target, _ = normalize(needle)
    found: list[tuple[int, int]] = []
    if not target:
        return found
    position = folded.find(target)
    while position >= 0:
        first = spans[position]
        last = spans[position + len(target) - 1]
        found.append((first[0], last[1]))
        position = folded.find(target, position + 1)
    return found


# ---------------------------------------------------------------- generator
CJK_POOL = (
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对"
    "小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动"
    "电吉他报销预算三千五百买餐厅饭店价钱來說這後報銷電預買廳價"
)
LATIN = [
    "iPhone15Pro",
    "RTX4090",
    "PR #448",
    "ＰＲ＃４４８",
    "PR#448",
    "macOS",
    "Wi-Fi",
    "https://example.com/a?b=1",
    "github.com/YuanYeYouTao/Yuki",
    "3k",
    "3000",
    "v3.9.0",
]
EMOJI = ["😀", "👍🏻", "🎸", "[QQ表情:12]", "é", "ｶﾞ", "㎏", "ﬁ"]
PUNCT = list("，。！？、；：“”（）《》#@-_/.,!?~ 　\n")


def generate(count: int, seed: int) -> list[tuple[int, int, str]]:
    rng = random.Random(seed)
    words = []
    for _ in range(4000):
        size = rng.choice([1, 2, 2, 2, 3, 4])
        words.append("".join(rng.choice(CJK_POOL) for _ in range(size)))
    weights = [1 / (rank + 1) for rank in range(len(words))]
    rows = []
    for event_id in range(1, count + 1):
        parts = rng.choices(words, weights=weights, k=rng.randint(1, 9))
        if rng.random() < 0.12:
            parts.insert(rng.randrange(len(parts) + 1), rng.choice(LATIN))
        if rng.random() < 0.08:
            parts.insert(rng.randrange(len(parts) + 1), rng.choice(EMOJI))
        for _ in range(rng.randint(0, 2)):
            parts.insert(rng.randrange(len(parts) + 1), rng.choice(PUNCT))
        rows.append((event_id, event_id % 20, "".join(parts)))
    return rows


# ---------------------------------------------------------------- index options
def hex_token(prefix: str, gram: str) -> str:
    return prefix + gram.encode("utf-8").hex()


def grams(text: str, sizes: tuple[int, ...], pad: bool = False) -> str:
    folded, _ = normalize(text)
    if pad:
        folded = folded + "\x00"
    tokens = []
    for size in sizes:
        tag = {1: "u", 2: "b", 3: "t"}[size]
        tokens.extend(hex_token(tag, folded[i : i + size]) for i in range(len(folded) - size + 1))
    return " ".join(tokens)


OPTIONS = {
    "raw_trigram": "current: built-in trigram over raw content",
    "ngram123": "one contentless-delete table, normalized 1/2/3-gram tokens",
    "trigram_plus_12": "contentless-delete trigram over normalized text + 1/2-gram table",
    "ngram23_prefix": "normalized 2/3-gram tokens, 1-char via bigram prefix (end padded)",
    "cjk12_words": "rejected design of the first draft: CJK 1/2-gram + alnum words",
}


def words_tokens(text: str) -> str:
    folded, _ = normalize(text)
    tokens, run, kind = [], "", None
    for ch in folded + "\x01":
        cjk = "㐀" <= ch <= "鿿"
        alnum = ch.isalnum() and not cjk
        current = "c" if cjk else "a" if alnum else None
        if current != kind and run:
            if kind == "c":
                tokens += [hex_token("u", c) for c in run]
                tokens += [hex_token("b", run[i : i + 2]) for i in range(len(run) - 1)]
            elif kind == "a":
                tokens.append(hex_token("w", run))
            run = ""
        if current:
            run += ch
        kind = current
    return " ".join(tokens)


def build(option: str, rows, path: Path) -> dict:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    # Rollback journal so the file size reflects committed pages without a checkpoint.
    db.execute("create table ev(id integer primary key, conv integer, content text)")
    db.execute("create index ix_ev_conv on ev(conv, id)")
    db.executemany("insert into ev values(?,?,?)", rows)
    db.commit()
    db.execute("vacuum")
    base = path.stat().st_size
    started = time.perf_counter()
    if option == "raw_trigram":
        db.execute(
            "create virtual table f using fts5(content, content='ev', content_rowid='id',"
            " tokenize='trigram')"
        )
        db.execute("insert into f(f) values('rebuild')")
    elif option == "trigram_plus_12":
        db.execute(
            "create virtual table f using fts5(n, content='', contentless_delete=1,"
            " tokenize='trigram')"
        )
        db.execute("create virtual table g using fts5(tok, content='', contentless_delete=1)")
        db.executemany(
            "insert into f(rowid, n) values(?,?)", ((i, normalize(c)[0]) for i, _, c in rows)
        )
        db.executemany(
            "insert into g(rowid, tok) values(?,?)", ((i, grams(c, (1, 2))) for i, _, c in rows)
        )
    else:
        db.execute("create virtual table f using fts5(tok, content='', contentless_delete=1)")
        maker = {
            "ngram123": lambda c: grams(c, (1, 2, 3)),
            "ngram23_prefix": lambda c: grams(c, (2, 3), pad=True),
            "cjk12_words": words_tokens,
        }[option]
        db.executemany("insert into f(rowid, tok) values(?,?)", ((i, maker(c)) for i, _, c in rows))
    db.commit()
    build_seconds = time.perf_counter() - started
    db.execute("vacuum")
    return {
        "db": db,
        "index_mb": round((path.stat().st_size - base) / 1e6, 2),
        "build_seconds": round(build_seconds, 2),
        "base_mb": round(base / 1e6, 2),
    }


def candidates(option: str, db, term: str, conv: int | None = None) -> set[int]:
    folded, _ = normalize(term)

    def scoped(fts_sql: str) -> str:
        if conv is None:
            return fts_sql
        return f"select id from ev where id in ({fts_sql}) and conv={conv}"

    if option == "raw_trigram":
        raw = term.strip()
        if len(raw) >= 3:
            sql = scoped("select rowid from f where f match ?")
            return {r[0] for r in db.execute(sql, ('"' + raw.replace('"', '""') + '"',))}
        sql = "select id from ev where instr(content, ?) > 0" + (
            "" if conv is None else f" and conv={conv}"
        )
        return {r[0] for r in db.execute(sql, (raw,))}
    if option == "trigram_plus_12":
        if len(folded) >= 3:
            sql = scoped("select rowid from f where f match ?")
            return {r[0] for r in db.execute(sql, ('"' + folded.replace('"', '""') + '"',))}
        tag = "u" if len(folded) == 1 else "b"
        return {
            r[0]
            for r in db.execute(
                scoped("select rowid from g where g match ?"), ('"' + hex_token(tag, folded) + '"',)
            )
        }
    if option == "ngram123" or option == "ngram23_prefix":
        if len(folded) >= 3:
            toks = {hex_token("t", folded[i : i + 3]) for i in range(len(folded) - 2)}
            expr = " AND ".join(f'"{t}"' for t in sorted(toks))
        elif len(folded) == 2:
            expr = '"{}"'.format(hex_token("b", folded))
        elif option == "ngram123":
            expr = '"{}"'.format(hex_token("u", folded))
        else:
            expr = '"{}"*'.format(hex_token("b", folded))
        return {r[0] for r in db.execute(scoped("select rowid from f where f match ?"), (expr,))}
    if option == "cjk12_words":
        toks = words_tokens(term).split()
        if not toks:
            return set()
        expr = " AND ".join(f'"{t}"' for t in sorted(set(toks)))
        return {r[0] for r in db.execute(scoped("select rowid from f where f match ?"), (expr,))}
    raise ValueError(option)


def make_queries(rows, seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    queries = [
        "phone",
        "15pro",
        "iphone15",
        "#448",
        "pr#448",
        "PR #448",
        "ＰＲ＃４４８",
        "448",
        "example.com",
        "a?b=1",
        "wi-fi",
        "fi",
        "kg",
        "é",
        "ガ",
        "😀",
        "👍",
        "🎸",
        "报销",
        "報銷",
        "电",
        "電吉他",
        "三千",
        "3k",
        "v3.9",
        "表情:12",
        "，",
        "？",
        "預算三千",
        "后来",
    ]
    while len(queries) < count:
        _, _, content = rng.choice(rows)
        folded, _ = normalize(content)
        if not folded:
            continue
        size = rng.choice([1, 1, 2, 2, 3, 4, 5, 8])
        start = rng.randrange(max(1, len(folded) - size + 1))
        piece = folded[start : start + size]
        if piece:
            queries.append(piece)
    return queries


def timed(fn, repeats: int) -> tuple[float, float]:
    fn()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return round(statistics.median(samples), 3), round(
        samples[max(0, int(len(samples) * 0.95) - 1)], 3
    )


# ---------------------------------------------------------------- probes
def probe_index_options(rows, workdir: Path, query_count: int, repeats: int) -> dict:
    folded_rows = {i: normalize(c)[0] for i, _, c in rows}
    queries = make_queries(rows, 5, query_count)
    truth = {}
    for q in queries:
        target = normalize(q)[0]
        truth[q] = {i for i, f in folded_rows.items() if target and target in f}
    results = {}
    for option in OPTIONS:
        built = build(option, rows, workdir / f"{option}.db")
        db = built["db"]
        misses, missed_queries, cand_total, truth_total = 0, [], 0, 0
        for q in queries:
            got = candidates(option, db, q)
            lost = truth[q] - got
            if lost:
                misses += 1
                if len(missed_queries) < 12:
                    missed_queries.append(q)
            cand_total += len(got)
            truth_total += len(truth[q])
        latency = {}
        for label, q in (
            ("one_char_common", "的"),
            ("two_char", "报销"),
            ("long_cjk", "预算三千"),
            ("latin_inner", "phone"),
            ("punct_mixed", "pr#448"),
        ):
            latency[label] = timed(
                lambda q=q, option=option, db=db: candidates(option, db, q, conv=3), repeats
            )
        batch_ms = []
        if option in ("ngram123", "trigram_plus_12"):
            db.commit()
            db.isolation_level = None
            for batch in range(20):
                chunk = rows[batch * 500 : (batch + 1) * 500]
                if option == "ngram123":
                    prepared = [(i, grams(c, (1, 2, 3))) for i, _, c in chunk]
                else:
                    prepared = [(i, normalize(c)[0], grams(c, (1, 2))) for i, _, c in chunk]
                started = time.perf_counter()
                db.execute("begin immediate")
                if option == "ngram123":
                    db.executemany("insert or replace into f(rowid, tok) values(?,?)", prepared)
                else:
                    db.executemany(
                        "insert or replace into f(rowid, n) values(?,?)",
                        [(i, n) for i, n, _ in prepared],
                    )
                    db.executemany(
                        "insert or replace into g(rowid, tok) values(?,?)",
                        [(i, t) for i, _, t in prepared],
                    )
                db.execute("commit")
                batch_ms.append((time.perf_counter() - started) * 1000)
            batch_ms.sort()
            db.isolation_level = ""
        write_rows = [(len(rows) + 1 + k, k % 20, rows[k][2]) for k in range(2000)]
        started = time.perf_counter()
        for event_id, conv, content in write_rows:
            db.execute("insert into ev values(?,?,?)", (event_id, conv, content))
            if option == "raw_trigram":
                db.execute("insert into f(rowid, content) values(?,?)", (event_id, content))
            elif option == "trigram_plus_12":
                db.execute("insert into f(rowid,n) values(?,?)", (event_id, normalize(content)[0]))
                db.execute(
                    "insert into g(rowid,tok) values(?,?)", (event_id, grams(content, (1, 2)))
                )
            else:
                maker = {
                    "ngram123": lambda c: grams(c, (1, 2, 3)),
                    "ngram23_prefix": lambda c: grams(c, (2, 3), pad=True),
                    "cjk12_words": words_tokens,
                }[option]
                db.execute("insert into f(rowid,tok) values(?,?)", (event_id, maker(content)))
            db.commit()
        per_message_ms = (time.perf_counter() - started) * 1000 / len(write_rows)
        db.close()
        results[option] = {
            "description": OPTIONS[option],
            "index_mb": built["index_mb"],
            "base_table_mb": built["base_mb"],
            "bulk_build_seconds": built["build_seconds"],
            "incremental_insert_ms_per_message_with_commit": round(per_message_ms, 3),
            "reindex_batch_500_write_txn_ms_p50_max": (
                [round(statistics.median(batch_ms), 2), round(batch_ms[-1], 2)]
                if batch_ms
                else None
            ),
            "queries": len(queries),
            "queries_with_missed_matches": misses,
            "examples_missed": missed_queries,
            "candidate_to_truth_ratio": round(cand_total / max(1, truth_total), 3),
            "latency_ms_p50_p95_conv3": latency,
        }
        for suffix in ("", "-wal", "-shm"):
            target = workdir / f"{option}.db{suffix}"
            if target.exists():
                target.unlink()
    return results


def probe_contentless_ops() -> dict:
    db = sqlite3.connect(":memory:")
    db.execute("create virtual table f using fts5(tok, content='', contentless_delete=1)")
    db.executemany("insert into f(rowid, tok) values(?,?)", [(1, "aa bb"), (2, "bb cc"), (3, "cc")])
    out = {}

    def attempt(label, sql, args=()):
        try:
            db.execute(sql, args)
            db.commit()
            out[label] = "ok"
        except sqlite3.Error as exc:
            out[label] = f"error: {exc}"

    attempt("delete_by_rowid", "delete from f where rowid=1")
    attempt("insert_or_replace", "insert or replace into f(rowid, tok) values(2, 'dd')")
    out["after_replace_match_dd"] = [
        r[0] for r in db.execute("select rowid from f where f match 'dd'")
    ]
    out["after_replace_match_bb"] = [
        r[0] for r in db.execute("select rowid from f where f match 'bb'")
    ]
    attempt("integrity_check", "insert into f(f, rank) values('integrity-check', 0)")
    attempt("optimize", "insert into f(f) values('optimize')")
    attempt("rebuild", "insert into f(f) values('rebuild')")
    attempt("delete_command", "insert into f(f, rowid, tok) values('delete', 3, 'cc')")
    attempt("delete_all", "insert into f(f) values('delete-all')")
    out["rows_after_delete_all"] = db.execute(
        "select count(*) from f where f match 'cc OR dd'"
    ).fetchone()[0]
    attempt("select_column_value", "select tok from f")
    out["select_column_value_result"] = db.execute("select tok from f").fetchall()[:1]
    db.execute("create table ev(id integer primary key, body text)")
    db.execute(
        "create trigger ev_ad after delete on ev begin delete from f where rowid=old.id; end"
    )
    db.execute("insert into ev values(9,'x')")
    db.execute("insert into f(rowid,tok) values(9,'zz')")
    db.execute("delete from ev where id=9")
    out["trigger_delete_leaves_match"] = [
        r[0] for r in db.execute("select rowid from f where f match 'zz'")
    ]
    return out


def probe_enqueue_lock(workdir: Path, count: int) -> dict:
    path = workdir / "enqueue.db"
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("create table ev(id integer primary key, body text)")
    db.execute("begin")
    db.executemany("insert into ev values(?, ?)", ((i, "x" * 30) for i in range(1, count + 1)))
    db.execute("commit")
    db.execute("create table pending(event_id integer primary key, seq integer not null)")
    started = time.perf_counter()
    db.execute("begin immediate")
    db.execute("insert into pending(event_id, seq) select id, 0 from ev")
    db.execute("commit")
    single = time.perf_counter() - started
    db.execute("delete from pending")
    batch_times = []
    cursor = 0
    while True:
        started = time.perf_counter()
        db.execute("begin immediate")
        db.execute(
            "insert into pending(event_id, seq) select id, 0 from ev where id > ? "
            "order by id limit 5000",
            (cursor,),
        )
        last = db.execute("select max(event_id) from pending").fetchone()[0]
        db.execute("commit")
        batch_times.append((time.perf_counter() - started) * 1000)
        if last is None or last == cursor:
            break
        cursor = last
    db.close()
    for suffix in ("", "-wal", "-shm"):
        target = workdir / f"enqueue.db{suffix}"
        if target.exists():
            target.unlink()
    batch_times.sort()
    return {
        "rows": count,
        "single_insert_select_write_lock_seconds": round(single, 3),
        "keyset_batches": len(batch_times),
        "batch_5000_write_lock_ms_p50": round(statistics.median(batch_times), 2),
        "batch_5000_write_lock_ms_max": round(batch_times[-1], 2),
    }


def probe_bm25() -> dict:
    db = sqlite3.connect(":memory:")
    db.execute("create virtual table f using fts5(tok)")
    filler = [(100 + i, f"filler text number {i}") for i in range(200)]
    db.executemany(
        "insert into f(rowid,tok) values(?,?)",
        [
            (1, "guitar"),
            (2, "guitar guitar budget"),
            (3, "budget now long long long text here"),
            *filler,
        ],
    )
    order = db.execute(
        "select rowid, bm25(f) from f where f match 'guitar' order by rank"
    ).fetchall()
    before = dict(db.execute("select rowid, bm25(f) from f where f match 'guitar'").fetchall())
    db.executemany("insert into f(tok) values(?)", [("unrelated words",)] * 50)
    after = dict(db.execute("select rowid, bm25(f) from f where f match 'guitar'").fetchall())
    # Group A: one message covering both terms.  Group B: two messages.
    db.execute("create virtual table g using fts5(tok)")
    db.executemany(
        "insert into g(rowid,tok) values(?,?)",
        [(10, "guitar budget"), (20, "guitar"), (21, "budget"), *filler],
    )
    score = dict(
        db.execute("select rowid, bm25(g) from g where g match 'guitar OR budget'").fetchall()
    )
    return {
        "order_by_rank_returns_most_relevant_first": order,
        "score_sign": "negative; lower is better (ORDER BY rank ascending)",
        "same_doc_scores_before_after_inserting_50_unrelated_docs": {
            str(k): [round(before[k], 4), round(after[k], 4)] for k in before
        },
        "sum_aggregation": {
            "one_message_group_sum": round(score[10], 4),
            "two_message_group_sum": round(score[20] + score[21], 4),
            "note": "lower is better, so summing favours the group with more messages",
        },
    }


def minimal_windows(hits, terms, max_messages, max_seconds):
    """hits: list of (position, time_seconds, frozenset(terms)) sorted by position."""

    need = len(terms)
    windows = []
    count = Counter()
    left = 0
    for right, (_, _, have) in enumerate(hits):
        for t in have:
            count[t] += 1
        while len([t for t in terms if count[t] > 0]) == need:
            l_pos, l_time, l_have = hits[left]
            if all(count[t] - (1 if t in l_have else 0) > 0 for t in terms):
                for t in l_have:
                    count[t] -= 1
                left += 1
                continue
            r_pos, r_time, r_have = hits[right]
            right_needed = any(count[t] == 1 for t in r_have if t in terms)
            if right_needed and r_pos - l_pos <= max_messages and r_time - l_time <= max_seconds:
                windows.append((l_pos, r_pos))
            break
    return windows


def probe_grouping() -> dict:
    hits = [(p, p * 60, frozenset({"A"} if p <= 12 else {"B"})) for p in range(1, 26)]
    terms = ("A", "B")
    new = minimal_windows(hits, terms, max_messages=20, max_seconds=1800)

    # First-draft algorithm: merge all linked hits, split at the largest gap when
    # the fragment exceeds 24 events, then drop pieces that do not cover all terms.
    def old_algorithm(max_events=24):
        pieces = [hits]
        out = []
        while pieces:
            piece = pieces.pop()
            if len(piece) > max_events:
                gaps = [(piece[i + 1][1] - piece[i][1], i) for i in range(len(piece) - 1)]
                _, cut = max(gaps)  # every gap equal: max picks the last index
                gaps_at_12 = cut
                pieces += [piece[: cut + 1], piece[cut + 1 :]]
                del gaps_at_12
                continue
            covered = set().union(*(h[2] for h in piece))
            if covered >= set(terms):
                out.append((piece[0][0], piece[-1][0]))
        return out

    # Same but with the largest gap explicitly between 12 and 13, as in the review.
    hits_gap = [
        (p, p * 60 + (600 if p >= 13 else 0), frozenset({"A"} if p <= 12 else {"B"}))
        for p in range(1, 26)
    ]

    def old_with_gap(max_events=24):
        piece = hits_gap
        gaps = [(piece[i + 1][1] - piece[i][1], i) for i in range(len(piece) - 1)]
        _, cut = max(gaps)
        parts = [piece[: cut + 1], piece[cut + 1 :]]
        return [(p[0][0], p[-1][0]) for p in parts if set().union(*(h[2] for h in p)) >= set(terms)]

    chain = [(p, p * 600, frozenset({"A"} if p % 2 else {"C"})) for p in range(1, 30)]
    chain += [(31, 31 * 600, frozenset({"B"}))]
    transitive = minimal_windows(chain, ("A", "B", "C"), max_messages=20, max_seconds=1800)
    aba = [(1, 0, frozenset({"A"})), (2, 60, frozenset({"B"})), (3, 120, frozenset({"A"}))]
    three = [
        (1, 0, frozenset({"A"})),
        (2, 60, frozenset({"B"})),
        (3, 120, frozenset({"A"})),
        (9, 480, frozenset({"C"})),
        (40, 2400, frozenset({"B", "C"})),
    ]
    return {
        "overlapping_A_B_A": minimal_windows(aba, ("A", "B"), 20, 1800),
        "three_terms_with_far_cover": minimal_windows(three, ("A", "B", "C"), 20, 1800),
        "counterexample_25_hits_new_minimal_windows": new,
        "counterexample_first_draft_split_drop": old_with_gap(),
        "counterexample_first_draft_equal_gaps": old_algorithm(),
        "chain_A_C_every_10min_then_B": transitive,
    }


def probe_paging(count: int, budget: int) -> dict:
    rng = random.Random(9)
    common = {i for i in range(1, count + 1) if rng.random() < 0.3}
    rare = {500, 40_000, count - 10}
    t_hits = {"common": sorted(common), "rare": sorted(rare)}

    def page(upper: int):
        fetched = {}
        floors = []
        for term, ids in t_hits.items():
            below = [i for i in ids if i < upper][::-1][:budget]
            fetched[term] = below
            floors.append(below[-1] if len(below) == budget else 0)
        lower = max(floors)
        return lower, {t: [i for i in v if i > lower] for t, v in fetched.items()}

    upper, requests, found = count + 1, 0, []
    while upper > 1 and requests < 10_000:
        lower, complete = page(upper)
        requests += 1
        found += complete["rare"]
        if lower >= upper:
            break
        upper = lower + 1 if lower else 1
    naive = sorted(common | rare, reverse=True)[:budget]
    return {
        "messages": count,
        "budget_per_term": budget,
        "keyset_requests_to_exhaust": requests,
        "keyset_rare_hits_reached": sorted(found),
        "naive_top_budget_contains_rare": sorted(rare & set(naive)),
    }


def probe_trigram_semantics() -> dict:
    db = sqlite3.connect(":memory:")
    db.execute("create table ev(id integer primary key, content text)")
    db.execute(
        "create virtual table f using fts5(content, content='ev', content_rowid='id',"
        " tokenize='trigram')"
    )
    rows = [
        (1, "我想买电吉他"),
        (2, "我先去吃饭"),
        (3, "预算大概三千"),
        (4, "报销单交了吗"),
        (5, "報銷流程在哪"),
        (6, "PR #448 合了"),
        (7, "ＰＲ＃４４８ 还没review"),
        (8, "不买电吉他了，预算三千不够"),
        (9, "iPhone15Pro 价格"),
    ]
    db.executemany("insert into ev values(?,?)", rows)
    db.execute("insert into f(f) values('rebuild')")
    out = {}
    for query in [
        '"电吉他 三千"',
        "电吉他 三千",
        '"电吉他" AND "三千"',
        '"三千"',
        '"报销"',
        '"報銷单"',
        '"报销单"',
        '"PR #448"',
        '"pr #448"',
        '"phone"',
        '"电吉他" NOT "不买"',
        '"电吉他" NOT "不买电"',
    ]:
        try:
            out[query] = [
                r[0]
                for r in db.execute("select rowid from f where f match ? order by rowid", (query,))
            ]
        except sqlite3.Error as exc:
            out[query] = f"error: {exc}"
    return out


def probe_v0_invariant(rows, seed: int, count: int) -> dict:
    """casefold_ascii_v0: ASCII-lowered raw substring, trigram >= 3 chars, instr below."""

    sample = rows[:50_000]
    db = sqlite3.connect(":memory:")
    db.execute("create table ev(id integer primary key, content text)")
    db.execute(
        "create virtual table f using fts5(content, content='ev', content_rowid='id',"
        " tokenize='trigram')"
    )
    db.executemany("insert into ev values(?,?)", [(i, c) for i, _, c in sample])
    db.execute("insert into f(f) values('rebuild')")

    def ascii_lower(text: str) -> str:
        return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)

    rng = random.Random(seed)
    queries = ["PR #448", "pr #448", "IPHONE", "phone", "电", "报销", "Ä", "ß"]
    while len(queries) < count:
        _, _, content = rng.choice(sample)
        size = rng.choice([1, 2, 3, 4, 6])
        start = rng.randrange(max(1, len(content) - size + 1))
        piece = content[start : start + size].strip()
        if piece:
            queries.append(piece if rng.random() < 0.7 else piece.upper())
    lowered = {i: ascii_lower(c) for i, _, c in sample}
    misses = []
    for q in queries:
        term = ascii_lower(q.strip())
        truth = {i for i, c in lowered.items() if term in c}
        if len(q.strip()) >= 3:
            got = {
                r[0]
                for r in db.execute(
                    "select rowid from f where f match ?",
                    ('"' + q.strip().replace('"', '""') + '"',),
                )
            }
        else:
            got = {
                r[0]
                for r in db.execute(
                    "select id from ev where instr(lower(content), lower(?)) > 0", (q.strip(),)
                )
            }
        if truth - got:
            misses.append(q)
    return {
        "rows": len(sample),
        "queries": len(queries),
        "queries_with_missed_matches": len(misses),
        "examples_missed": misses[:10],
    }


def probe_time_binding() -> dict:
    from datetime import datetime, timedelta, timezone

    out = {}
    db = sqlite3.connect(":memory:")
    db.execute("create table ev(id integer primary key, occurred_at text)")
    # SQLAlchemy's SQLite DateTime storage drops tzinfo; Yuki stores UTC values.
    db.execute("insert into ev values(1, '2026-10-09 16:30:00.000000')")
    sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
    for label in ["2026-10-10T00:00:00+08:00", "2026-10-10T00:00:00", "2026-10-09T16:00:00Z"]:
        value = datetime.fromisoformat(label.replace("Z", "+00:00"))
        out[label] = db.execute(
            "select count(*) from ev where occurred_at >= ?", (value,)
        ).fetchone()[0]
    out["stored_row_is_utc_for_local"] = "2026-10-10T00:30:00+08:00"
    fixed = datetime(2026, 10, 10, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    out["after_converted_to_naive_utc_text"] = db.execute(
        "select count(*) from ev where occurred_at >= ?",
        (fixed.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f"),),
    ).fetchone()[0]
    try:
        from sqlalchemy import Column, DateTime, Integer, create_engine, text
        from sqlalchemy.orm import Session, declarative_base

        base = declarative_base()

        class Event(base):
            __tablename__ = "e"
            id = Column(Integer, primary_key=True)
            t = Column(DateTime(timezone=True))

        engine = create_engine("sqlite://")
        base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Event(id=1, t=datetime(2026, 10, 9, 16, 30, tzinfo=UTC)))
            session.commit()
            out["sqlalchemy_stored"] = session.execute(text("select t from e")).scalar()
            out["sqlalchemy_text_bind_after_+08:00"] = session.execute(
                text("select count(*) from e where t >= :a"),
                {"a": datetime.fromisoformat("2026-10-10T00:00:00+08:00")},
            ).scalar()
    except ImportError:
        out["sqlalchemy"] = "not installed"
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=200_000)
    parser.add_argument("--queries", type=int, default=400)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--enqueue-rows", type=int, default=1_000_000)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)
    rows = generate(args.messages, seed=448)
    result = {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "cpu": os.environ.get("PROBE_CPU", ""),
            "messages": args.messages,
            "queries": args.queries,
            "repeats": args.repeats,
            "seed": 448,
        },
        "trigram_semantics": probe_trigram_semantics(),
        "v0_invariant": probe_v0_invariant(rows, 17, 300),
        "time_binding": probe_time_binding(),
        "highlight_examples": {
            "PR #448 in 'see ＰＲ＃４４８ ok'": highlight("see ＰＲ＃４４８ ok", "PR #448"),
            "k in '5㎏'": highlight("5㎏", "k"),
            "é in 'café!'": highlight("café!", "é"),
            "报销 in '報 銷單'": highlight("報 銷單", "报销"),
            "ガ in 'ｶﾞｲﾄﾞ'": highlight("ｶﾞｲﾄﾞ", "ガ"),
        },
        "index_options": probe_index_options(rows, args.workdir, args.queries, args.repeats),
        "contentless_delete_operations": probe_contentless_ops(),
        "enqueue_write_lock": probe_enqueue_lock(args.workdir, args.enqueue_rows),
        "bm25": probe_bm25(),
        "grouping": probe_grouping(),
        "paging": probe_paging(200_000, 2000),
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
