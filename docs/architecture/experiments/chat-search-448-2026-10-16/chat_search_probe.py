"""Synthetic probes for the #448 chat-search taskbook.

Standard library + sqlite3.  The snapshot probe imports Yuki's ``Database`` and
the revision probe runs Yuki's 0119 trigger SQL; nothing else uses Yuki code.  No
real chat data, no network.  Every random choice is seeded, so a rerun on the
same Python, Unicode and SQLite version reproduces the counts; timings vary.

Usage (from the repository root, with the repository virtualenv):
    .venv/bin/python -I chat_search_probe.py --workdir /tmp/x --out results.json
"""

from __future__ import annotations

import argparse
import itertools
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


def fold_whole(text: str) -> str:
    """Independent oracle: the v1 pipeline applied to the whole string at once."""

    folded = unicodedata.normalize("NFKC", text)
    folded = unicodedata.normalize("NFKC", folded.casefold())
    folded = folded.translate(T2S)
    return "".join(ch for ch in folded if not ch.isspace())


def _composition_seconds() -> frozenset[str]:
    """Characters that can be the second element of a canonical composition."""

    seconds = set()
    for code_point in range(0x110000):
        decomposition = unicodedata.decomposition(chr(code_point))
        if decomposition and not decomposition.startswith("<"):
            parts = decomposition.split()
            if len(parts) == 2:
                seconds.add(chr(int(parts[1], 16)))
    # Hangul syllables compose algorithmically and have no decomposition entry.
    seconds.update(chr(code_point) for code_point in range(0x1161, 0x1176))
    seconds.update(chr(code_point) for code_point in range(0x11A8, 0x11C3))
    return frozenset(seconds)


COMPOSITION_SECONDS = _composition_seconds()


def safe_start(ch: str) -> bool:
    """True when no normalization step can interact with text before ``ch``.

    UAX #15 section 1.4: normalization is not closed under concatenation.  A cut
    before ``ch`` is safe when ``ch``, its NFKD form and the NFKD form after
    casefolding all begin with a starter that never composes with a preceding
    character.  Being conservative only makes highlight units larger.
    """

    for form in (
        ch,
        unicodedata.normalize("NFKD", ch),
        unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", ch).casefold()),
    ):
        if form and (unicodedata.combining(form[0]) != 0 or form[0] in COMPOSITION_SECONDS):
            return False
    return True


def normalize(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Fold per safe unit; return folded text and each output char's source span."""

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    cuts = [0, *(i for i in range(1, len(text)) if safe_start(text[i])), len(text)]
    for begin, finish in itertools.pairwise(cuts):
        folded = fold_whole(text[begin:finish])
        out.append(folded)
        spans.extend([(begin, finish)] * len(folded))
    return "".join(out), spans


def greedy_normalize_first_revision(text: str) -> str:
    """The second-draft method kept only to reproduce its counterexample."""

    out = []
    index = 0
    while index < len(text):
        end = index + 1
        while end < len(text) and fold_whole(text[index : end + 1]) != fold_whole(
            text[index:end]
        ) + fold_whole(text[end]):
            end += 1
        out.append(fold_whole(text[index:end]))
        index = end
    return "".join(out)


def highlight(original: str, needle: str) -> list[tuple[int, int]]:
    folded, spans = normalize(original)
    target = fold_whole(needle)
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
    # The answer key uses the whole-string oracle, not the segmented normalizer.
    folded_rows = {i: fold_whole(c) for i, _, c in rows}
    queries = make_queries(rows, 5, query_count)
    truth = {}
    for q in queries:
        target = fold_whole(q)
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


# ---------------------------------------------------------------- round 3 probes
NORMALIZATION_CONTEXTS = [
    "",
    "a",
    "a̕",
    "é",
    "ᄀ",
    "가",
    "각",
    "ｶ",
    "カ",
    "େ",
    "ೆ",
    "ে",
    "q̣",
    " ",
    "报",
    "1",
]


def probe_normalization(seed: int, samples: int) -> dict:
    x, y = "à̕", "à̕"
    pairs = [
        (x, y),
        ("é", "é"),
        ("q̣̇", "q̣̇"),
        ("Å", "Å"),
        ("ｶﾞ", "ガ"),
        ("가", "가"),
        ("ㄱㅏ", "가"),
        ("㎏", "kg"),
        ("ﬁ", "fi"),
        ("ß", "ss"),
        ("ＰＲ＃４４８", "pr#448"),
        ("報 銷", "报销"),
    ]
    pair_results = []
    for left, right in pairs:
        pair_results.append(
            {
                "pair": [left, right],
                "oracle_equal": fold_whole(left) == fold_whole(right),
                "segmented_equal": normalize(left)[0] == normalize(right)[0],
                "segmented_matches_oracle": normalize(left)[0] == fold_whole(left)
                and normalize(right)[0] == fold_whole(right),
                "right_finds_left": bool(fold_whole(right))
                and fold_whole(right) in normalize("<" + left + ">")[0],
            }
        )
    rng = random.Random(seed)
    pool = [
        chr(code_point)
        for code_point in [
            *range(0x300, 0x370),
            *range(0x1100, 0x1200),
            *range(0xAC00, 0xAC40),
            *range(0xFF61, 0xFFA0),
            *range(0x3130, 0x3190),
            *range(0x3099, 0x309D),
        ]
    ] + list("aeiouAEIOUßİﬁ㎏ 報报銷销ཱི̕ͅ")
    pool += list("ೕೆୋোเก้Å")
    random_mismatch = 0
    highlight_failures = 0
    for _ in range(samples):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 10)))
        folded = normalize(text)[0]
        if folded != fold_whole(text):
            random_mismatch += 1
            continue
        if folded:
            start = rng.randrange(len(folded))
            target = folded[start : start + rng.randint(1, 3)]
            for begin, finish in highlight(text, target):
                if fold_whole(target) not in fold_whole(text[begin:finish]):
                    highlight_failures += 1
    assigned = [
        chr(code_point)
        for code_point in range(0x110000)
        if unicodedata.category(chr(code_point)) not in {"Cn", "Cs", "Co"}
    ]
    safe = [ch for ch in assigned if safe_start(ch)]
    boundary_failures = []
    for ch in safe:
        for context in NORMALIZATION_CONTEXTS:
            if fold_whole(context + ch) != fold_whole(context) + fold_whole(ch):
                boundary_failures.append([context, f"U+{ord(ch):04X}"])
    return {
        "counterexample": {
            "x": "a U+0315 U+0300",
            "y": "U+00E0 U+0315",
            "nfkc_equal": unicodedata.normalize("NFKC", x) == unicodedata.normalize("NFKC", y),
            "second_draft_equal": greedy_normalize_first_revision(x)
            == greedy_normalize_first_revision(y),
            "second_draft_y_finds_x": greedy_normalize_first_revision(y)
            in greedy_normalize_first_revision(x),
            "revised_equal": normalize(x)[0] == normalize(y)[0],
            "revised_y_finds_x": fold_whole(y) in normalize(x)[0],
        },
        "equivalence_pairs": pair_results,
        "random_strings": samples,
        "random_segmented_vs_oracle_mismatches": random_mismatch,
        "highlight_spans_not_containing_target": highlight_failures,
        "assigned_code_points": len(assigned),
        "safe_start_code_points": len(safe),
        "boundary_contexts": len(NORMALIZATION_CONTEXTS),
        "boundary_failures": boundary_failures[:10],
        "boundary_failure_count": len(boundary_failures),
    }


# Toy lifecycle schema mirroring taskbook section 7 (not Yuki's real tables).
TOY_SCHEMA = (
    "create table ev(id integer primary key, conv integer, kind text, content text, asr text)",
    "create virtual table idx using fts5(c_grams, a_grams, content='', contentless_delete=1)",
    "create table queue(seq integer primary key autoincrement, event_id integer not null)",
    "create index ix_queue_event on queue(event_id)",
    "create table state(id integer primary key check (id = 1), frontier integer not null,"
    " epoch integer not null)",
    "create trigger ev_ai after insert on ev when new.kind = 'message' begin"
    " insert into queue(event_id) values (new.id); end",
    "create trigger ev_au after update of content, asr on ev begin"
    " delete from idx where rowid = old.id; insert into queue(event_id) values (new.id); end",
    "create trigger ev_ad after delete on ev begin delete from idx where rowid = old.id;"
    " delete from queue where event_id = old.id; end",
)
# Shared by the "migration" path and the "create_schema" path.
TOY_STATE_INIT = (
    "insert into state(id, frontier, epoch) select 1, coalesce(max(id), 0) + 1, 0 from ev"
)


def toy_db(path: str = ":memory:", rows=(), init_before_rows: bool = False):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute(
        "create table ev(id integer primary key, conv integer, kind text, content text, asr text)"
    )
    if not init_before_rows:
        db.executemany("insert into ev values(?,?,?,?,?)", rows)
    for statement in TOY_SCHEMA[1:]:
        db.execute(statement)
    db.execute(TOY_STATE_INIT)
    if init_before_rows:
        db.executemany("insert into ev values(?,?,?,?,?)", rows)
    return db


def _tokens(text: str | None) -> str:
    folded = normalize(text or "")[0]
    out = []
    for size, tag in ((1, "u"), (2, "b"), (3, "t")):
        out.extend(hex_token(tag, folded[i : i + size]) for i in range(len(folded) - size + 1))
    return " ".join(out)


def indexer_round(
    db, batch: int, rule: str = "revised", between=None, conditional_write: bool = True
) -> int:
    """One indexer pass. ``between`` runs after the read, before the write.

    Tokens are prepared outside the write transaction.  The write is conditional on
    the event still existing, checked inside the same short write transaction.
    ``conditional_write=False`` reproduces the unconditional write.
    """

    if rule == "second_draft":
        rows = db.execute(
            "select seq, event_id from queue order by seq limit ?", (batch,)
        ).fetchall()
        work = []
        for seq, event_id in rows:
            newer = db.execute(
                "select 1 from queue where event_id = ? and seq > ?", (event_id, seq)
            ).fetchone()
            if newer is None:
                work.append((event_id, seq))
    else:
        work = db.execute(
            "select event_id, max(seq) from queue where event_id in"
            " (select event_id from queue order by seq limit ?) group by event_id",
            (batch,),
        ).fetchall()
    texts = {
        event_id: db.execute("select content, asr from ev where id = ?", (event_id,)).fetchone()
        for event_id, _ in work
    }
    prepared = {
        event_id: (None if row is None else (_tokens(row[0]), _tokens(row[1])))
        for event_id, row in texts.items()
    }
    if between is not None:
        between(db)
    db.execute("begin immediate")
    for event_id, seq in work:
        if prepared[event_id] is not None and conditional_write:
            db.execute(
                "insert or replace into idx(rowid, c_grams, a_grams)"
                " select ?, ?, ? where exists (select 1 from ev where id = ?)",
                (event_id, *prepared[event_id], event_id),
            )
        elif prepared[event_id] is not None:
            db.execute(
                "insert or replace into idx(rowid, c_grams, a_grams) values (?,?,?)",
                (event_id, *prepared[event_id]),
            )
        db.execute("delete from queue where event_id = ? and seq <= ?", (event_id, seq))
    db.execute("commit")
    return len(work)


def backfill_round(db, batch: int, between=None, compare_and_set: bool = True) -> int:
    """One backfill step; returns the number of ids read (0 = nothing below frontier)."""

    frontier, epoch = db.execute("select frontier, epoch from state").fetchone()
    ids = [
        r[0]
        for r in db.execute(
            "select id from ev where kind = 'message' and id < ? order by id desc limit ?",
            (frontier, batch),
        )
    ]
    if not ids:
        return 0
    if between is not None:
        between(db)
    db.execute("begin immediate")
    if compare_and_set:
        moved = db.execute(
            "update state set frontier = ? where frontier = ? and epoch = ?",
            (min(ids), frontier, epoch),
        ).rowcount
    else:
        moved = db.execute("update state set frontier = ?", (min(ids),)).rowcount
    if moved:
        db.executemany("insert into queue(event_id) values (?)", [(i,) for i in ids])
    db.execute("commit")
    return len(ids)


def reset_index(db) -> None:
    """Corruption recovery: one short write transaction."""

    db.execute("begin immediate")
    db.execute("insert into idx(idx) values ('delete-all')")
    db.execute(
        "update state set frontier = (select coalesce(max(id), 0) + 1 from ev), epoch = epoch + 1"
    )
    db.execute("commit")


def toy_search(db, term: str, conv: int) -> tuple[set[int], dict]:
    """Candidate generation + verification inside one read transaction."""

    target = fold_whole(term)
    db.execute("begin")
    frontier = db.execute("select frontier from state").fetchone()[0]
    if len(target) >= 3:
        grams = sorted({hex_token("t", target[i : i + 3]) for i in range(len(target) - 2)})
    else:
        grams = [hex_token("u" if len(target) == 1 else "b", target)]
    expr = " AND ".join(f'"{g}"' for g in grams)
    indexed = {
        r[0]
        for column in ("c_grams", "a_grams")
        for r in db.execute(
            "select id from ev where id in (select rowid from idx where idx match ?)"
            " and conv = ? and id >= ?",
            (f"{{{column}}}: ({expr})", conv, frontier),
        )
    }
    queued = {
        r[0]
        for r in db.execute(
            "select distinct q.event_id from queue q join ev on ev.id = q.event_id"
            " where ev.conv = ? and ev.id >= ?",
            (conv, frontier),
        )
    }
    below = {
        r[0]
        for r in db.execute(
            "select id from ev where conv = ? and kind = 'message' and id < ?", (conv, frontier)
        )
    }
    candidates = indexed | queued | below
    hits = set()
    for event_id in candidates:
        row = db.execute("select content, asr from ev where id = ?", (event_id,)).fetchone()
        if row and any(target and target in fold_whole(field or "") for field in row):
            hits.add(event_id)
    db.execute("rollback")
    return hits, {"frontier": frontier, "queued": len(queued), "below_frontier": len(below)}


def toy_oracle(db, term: str, conv: int) -> set[int]:
    target = fold_whole(term)
    return {
        event_id
        for event_id, content, asr in db.execute(
            "select id, content, asr from ev where conv = ? and kind = 'message'", (conv,)
        )
        if target and (target in fold_whole(content or "") or target in fold_whole(asr or ""))
    }


def probe_queue() -> dict:
    out = {}
    for rule in ("second_draft", "revised"):
        for batch in (500, 1):
            rows = [(i, 1, "message", f"old {i}", None) for i in range(1, 501)]
            db = toy_db(rows=rows, init_before_rows=True)  # seq 1..500
            db.execute("update ev set content = 'new ' || id")  # seq 501..1000
            rounds = 0
            progress = []
            while db.execute("select count(*) from queue").fetchone()[0] and rounds < 2000:
                before = db.execute("select count(*) from queue").fetchone()[0]
                indexer_round(db, batch, rule)
                after = db.execute("select count(*) from queue").fetchone()[0]
                progress.append(before - after)
                rounds += 1
                if before == after and rounds > 3:
                    break
            left = db.execute("select count(*) from queue").fetchone()[0]
            stale_tokens = sum(
                1 for i in range(1, 501) if i not in toy_search(db, f"new {i}", 1)[0]
            )
            out[f"{rule}_batch_{batch}"] = {
                "rounds": rounds,
                "queue_left": left,
                "first_rounds_removed": progress[:3],
                "latest_text_not_found": stale_tokens,
            }
    # Re-enqueue between read and write must survive.  The event is inserted through
    # the trigger, so the indexer starts from a real pending item (seq 1).
    db = toy_db(rows=[(1, 1, "message", "alpha", None)], init_before_rows=True)
    trace = {"queue_at_start": [r[0] for r in db.execute("select seq from queue")]}

    def update_during(conn):
        conn.execute("update ev set asr = 'bravo' where id = 1")
        trace["queue_after_update"] = [r[0] for r in conn.execute("select seq from queue")]
        trace["found_before_write"] = {
            word: sorted(toy_search(conn, word, 1)[0]) for word in ("alpha", "bravo")
        }

    indexer_round(db, 10, "revised", between=update_during)
    trace["queue_after_first_write"] = [r[0] for r in db.execute("select seq from queue")]
    trace["index_has_bravo_after_first_write"] = bool(
        db.execute(
            "select rowid from idx where idx match ?",
            (f'"{hex_token("t", fold_whole("bravo")[:3])}"',),
        ).fetchall()
    )
    trace["found_after_first_write"] = {
        word: sorted(toy_search(db, word, 1)[0]) for word in ("alpha", "bravo")
    }
    indexer_round(db, 10, "revised")
    trace["queue_after_second_round"] = [r[0] for r in db.execute("select seq from queue")]
    trace["found_after_second_round"] = {
        word: sorted(toy_search(db, word, 1)[0]) for word in ("alpha", "bravo")
    }
    trace["index_has_bravo_after_second_round"] = bool(
        db.execute(
            "select rowid from idx where idx match ?",
            (f'"{hex_token("t", fold_whole("bravo")[:3])}"',),
        ).fetchall()
    )
    out["reenqueue_during_processing"] = trace
    # Delete between read and write must not leave an orphan index row.
    for label, flag in (("unconditional_write", False), ("conditional_write", True)):
        db = toy_db(rows=[(1, 1, "message", "alpha", None)], init_before_rows=True)
        queue_at_start = [r[0] for r in db.execute("select seq from queue")]
        indexer_round(
            db,
            10,
            "revised",
            between=lambda conn: conn.execute("delete from ev where id = 1"),
            conditional_write=flag,
        )
        out[f"delete_during_processing_{label}"] = {
            "queue_at_start": queue_at_start,
            "ledger_rows": db.execute("select count(*) from ev").fetchone()[0],
            "queue_rows": db.execute("select count(*) from queue").fetchone()[0],
            "orphan_index_rowids": [
                r[0]
                for r in db.execute("select rowid from idx where rowid not in (select id from ev)")
            ],
            "search_alpha": sorted(toy_search(db, "alpha", 1)[0]),
        }
    # Duplicate queue rows must not consume the search budget.
    db = toy_db(rows=[(i, 1, "message", "x", None) for i in range(1, 6)])
    for _ in range(3):
        db.execute("update ev set content = content || 'y'")
    raw = db.execute("select event_id from queue order by seq desc limit 5").fetchall()
    distinct = db.execute(
        "select distinct event_id from queue order by event_id desc limit 5"
    ).fetchall()
    out["queue_scan_budget_5"] = {
        "raw_rows_distinct_events": len({r[0] for r in raw}),
        "distinct_rows_distinct_events": len(distinct),
    }
    return out


def probe_lifecycle(seed: int) -> dict:
    rng = random.Random(seed)
    words = ["电吉他", "报销", "報銷", "预算三千", "iPhone15Pro", "ＰＲ＃４４８", "à̕", "饭"]
    queries = ["电吉他", "报销", "phone", "pr#448", "à̕", "饭", "三千", "吉"]

    def make_rows(count, start=1):
        return [
            (
                start + i,
                rng.choice([1, 2]),
                "message" if rng.random() < 0.95 else "external_event",
                rng.choice(words) + rng.choice(["", "了", " 吗", "!"]),
                rng.choice([None, None, rng.choice(words)]),
            )
            for i in range(count)
        ]

    def check(db, label, results):
        mismatches = 0
        info = None
        for query in queries:
            for conv in (1, 2):
                got, info = toy_search(db, query, conv)
                if got != toy_oracle(db, query, conv):
                    mismatches += 1
        results[label] = {"mismatches": mismatches, **(info or {})}

    out = {}
    # Empty database, both init paths.
    for path_name, init_before in (("migration", False), ("create_schema", True)):
        results = {}
        db = toy_db(rows=[], init_before_rows=init_before)
        results["state_after_init"] = list(
            db.execute("select frontier, epoch from state").fetchone()
        )
        check(db, "empty", results)
        results["backfill_rows_on_empty"] = backfill_round(db, 100)
        db.execute("insert into ev values(1, 1, 'message', '电吉他', null)")
        check(db, "first_message_before_index", results)
        indexer_round(db, 100)
        check(db, "first_message_after_index", results)
        out[f"empty_{path_name}"] = results
    # Existing history, then migration, backfill and indexing interleaved.
    rows = make_rows(3000)
    results = {}
    db = toy_db(rows=rows)
    check(db, "before_backfill", results)
    steps = 0
    while backfill_round(db, 250):
        indexer_round(db, 200)
        db.executemany("insert into ev values(?,?,?,?,?)", make_rows(5, 100000 + steps * 10))
        steps += 1
        if steps in (1, 5):
            check(db, f"backfill_step_{steps}", results)
    results["backfill_requests"] = steps
    while indexer_round(db, 1000):
        pass
    check(db, "backfill_finished", results)
    results["rows_below_frontier_after_finish"] = db.execute(
        "select count(*) from ev where kind = 'message' and id < (select frontier from state)"
    ).fetchone()[0]
    # Corruption recovery: delete-all, reset frontier, bump epoch.
    reset_index(db)
    check(db, "after_delete_all", results)
    step = 0
    while backfill_round(db, 500):
        indexer_round(db, 300)
        step += 1
        if step == 2:
            check(db, "rebuild_in_progress", results)
    while indexer_round(db, 1000):
        pass
    check(db, "rebuild_finished", results)
    out["existing_history"] = results
    # A reset that commits between a backfill read and its write.
    history = make_rows(3000)
    for rule, flag in (("without_compare_and_set", False), ("compare_and_set", True)):
        results = {}
        db = toy_db(rows=history)
        for _ in range(6):  # index the newest half, frontier ends near the middle
            backfill_round(db, 250)
        while indexer_round(db, 1000):
            pass
        results["frontier_before_race"] = db.execute("select frontier from state").fetchone()[0]
        backfill_round(db, 250, between=reset_index, compare_and_set=flag)
        results["frontier_after_raced_write"] = db.execute("select frontier from state").fetchone()[
            0
        ]
        while backfill_round(db, 250, compare_and_set=flag):
            while indexer_round(db, 1000):
                pass
        while indexer_round(db, 1000):
            pass
        check(db, "after_rebuild", results)
        out[f"reset_during_backfill_{rule}"] = results
    return out


async def _snapshot_case(path: Path, explicit_begin: bool) -> dict:
    from sqlalchemy import text

    from qq_ai_bot.persistence.database import Database

    path.unlink(missing_ok=True)  # noqa: ASYNC240 - synchronous setup before any await
    database = Database(f"sqlite+aiosqlite:///{path}")
    async with database.sessions() as setup:
        for statement in (
            "create virtual table idx using fts5(tok, content='', contentless_delete=1)",
            "create table queue(seq integer primary key autoincrement, event_id integer)",
            "insert into queue(event_id) values (7)",
        ):
            await setup.execute(text(statement))
        await setup.commit()
    async with database.sessions() as reader, database.sessions() as writer:
        if explicit_begin:
            await reader.execute(text("BEGIN"))
        from_index = (
            await reader.execute(text("select rowid from idx where idx match 'zz'"))
        ).all()
        started = time.perf_counter()
        await writer.execute(text("insert into idx(rowid, tok) values (7, 'zz')"))
        await writer.execute(text("delete from queue where event_id = 7"))
        await writer.commit()
        writer_seconds = time.perf_counter() - started
        from_queue = (await reader.execute(text("select event_id from queue"))).all()
        await reader.rollback()
    await database.engine.dispose()
    return {
        "explicit_begin": explicit_begin,
        "index_saw_event": bool(from_index),
        "queue_saw_event": bool(from_queue),
        "event_found": bool(from_index) or bool(from_queue),
        "writer_committed_while_reader_open_seconds": round(writer_seconds, 4),
    }


def probe_snapshot(workdir: Path) -> dict:
    import asyncio

    import aiosqlite
    import sqlalchemy

    cases = [
        asyncio.run(_snapshot_case(workdir / "snapshot.db", explicit_begin=flag))
        for flag in (False, True)
    ]
    return {
        "versions": {
            "sqlalchemy": sqlalchemy.__version__,
            "aiosqlite": aiosqlite.__version__,
            "sqlite": sqlite3.sqlite_version,
        },
        "cases": cases,
    }


# ------------------------------------------------ paging mechanism simulation
def sim_groups(
    hits: dict[int, frozenset],
    pos: dict[int, int],
    times: dict[int, int],
    requirements: tuple,
    max_messages: int,
    max_seconds: int,
    evaluated: list | None = None,
) -> list[tuple[int, int]]:
    """Minimal covering windows (start_id, end_id) over hits sorted by id.

    ``evaluated`` collects the windows whose message distance had to be counted.
    """

    ordered = sorted(hits)
    if len(requirements) <= 1:
        return [(i, i) for i in ordered if hits[i] >= frozenset(requirements)]
    windows = []
    count: Counter = Counter()
    left = 0
    for rid in ordered:
        for req in hits[rid]:
            count[req] += 1
        while all(count[r] > 0 for r in requirements):
            lid = ordered[left]
            if all(count[r] - (1 if r in hits[lid] else 0) > 0 for r in requirements):
                for req in hits[lid]:
                    count[req] -= 1
                left += 1
                continue
            needed = any(count[r] == 1 for r in hits[rid])
            if needed and evaluated is not None:
                evaluated.append((lid, rid))
            if (
                needed
                and pos[rid] - pos[lid] <= max_messages
                and times[rid] - times[lid] <= max_seconds
            ):
                windows.append((lid, rid))
            break
    return windows


def sim_engine(dataset, requirements, budget, limit, max_messages, max_seconds, chunk=4):
    """Return pages, request count and per-page checks (recent order), section 6.7.

    Cost model (charged units): every candidate read is charged ``unit`` up front:
    1 source row + 1 original-text row + (M + 1) reserved for the one distance count
    in which it can be the right endpoint (multi-requirement only).  Each merge step
    also charges ``boundary`` = M + 1 for locating the event at pos(f) + M.  Actual
    rows are tracked separately and must never exceed the charged units.
    """

    ids, pos, times, sources = dataset  # sources: name -> (requirement, sorted ids)
    upper = (max(ids) + 1) if ids else 1
    single = len(requirements) <= 1
    margin = 0 if single else max_messages
    unit = 2 + (0 if single else max_messages + 1)
    boundary = 0 if single else max_messages + 1
    id_at = sorted(ids)
    cursor = (upper, upper)
    pages = []
    requests = 0
    checks = Counter()
    while cursor is not None and requests < 100000:
        requests += 1
        emit_upper, scan_upper = cursor
        charged = 0
        rows = Counter()
        known: dict[int, set] = {}
        verified: set[int] = set()
        counted: set[tuple[int, int]] = set()

        def read(name, event_ids, known=known, verified=verified, rows=rows):
            for i in event_ids:
                known.setdefault(i, set()).add(sources[name][0])
                rows["source"] += 1
                if i not in verified:
                    verified.add(i)
                    rows["verify"] += 1
            return unit * len(event_ids)

        window_reads = 0
        for name, (_, source_ids) in sources.items():
            window = [i for i in source_ids if scan_upper <= i < emit_upper]
            window_reads += len(window)
            charged += read(name, window)
        window_units = charged
        position = {name: scan_upper for name in sources}
        exhausted = {name: False for name in sources}
        first = True
        first_active = sum(1 for n in sources if not exhausted[n])
        state = None
        while True:
            active = [n for n in sources if not exhausted[n]]
            step_cost = len(active) * unit + boundary
            remaining = budget - charged
            if active and (first or remaining >= step_cost):
                fit = (remaining - boundary) // (len(active) * unit) if remaining > boundary else 0
                page = max(1, min(chunk, fit)) if first else min(chunk, fit)
                first = False
                for name in active:
                    below = [i for i in reversed(sources[name][1]) if i < position[name]][:page]
                    charged += read(name, below)
                    if below:
                        position[name] = below[-1]
                    if len(below) < page:
                        exhausted[name] = True
                charged += boundary
                rows["boundary"] += boundary
            active = [n for n in sources if not exhausted[n]]
            frontier = max((position[n] for n in active), default=0)
            complete = {i: frozenset(v) for i, v in known.items() if i >= frontier}
            evaluated: list = []
            groups = sim_groups(
                complete, pos, times, requirements, max_messages, max_seconds, evaluated
            )
            for lid, rid in evaluated:
                if (lid, rid) not in counted:
                    counted.add((lid, rid))
                    rows["distance"] += min(max_messages + 1, pos[rid] - pos[lid] + 1)
            if active:
                threshold_pos = pos[frontier] + margin if frontier in pos else 0
                safe = [g for g in groups if g[1] < emit_upper and pos[g[1]] >= threshold_pos]
            else:
                safe = [g for g in groups if g[1] < emit_upper]
            safe.sort(key=lambda g: g[1], reverse=True)
            if len(safe) >= limit + 1:
                state = "more"
                break
            if not active:
                state = "exhausted"
                break
            if budget - charged < len(active) * unit + boundary:
                state = "incomplete"
                break
        emitted = safe[:limit]
        pages.append((state, emitted))
        bound = max(budget, window_units + first_active * unit + boundary)
        checks["pages"] += 1
        checks["bound_violations"] += charged > bound
        checks["actual_rows_above_charged"] += sum(rows.values()) > charged
        checks["window_reads_above_nM"] += window_reads > len(sources) * margin
        checks["pages_window_exhausted_budget"] += window_units >= budget > 0
        checks["pages_sources_above_budget"] += first_active > budget
        checks["max_charged_over_budget"] = max(checks["max_charged_over_budget"], charged - budget)
        if state == "more":
            next_cursor = (emitted[-1][1], emitted[-1][1])
        elif state == "exhausted":
            next_cursor = None
        else:
            threshold_index = pos[frontier] + margin
            new_emit = id_at[threshold_index] if threshold_index < len(id_at) else upper
            next_cursor = (min(emit_upper, new_emit), frontier)
        if next_cursor is not None and not (
            next_cursor[0] < emit_upper
            or (next_cursor[0] == emit_upper and next_cursor[1] < scan_upper)
        ):
            checks["no_progress"] += 1
        cursor = next_cursor
    return pages, requests, checks


def sim_dataset(rng, size, densities, gap=True):
    ids = []
    current = 0
    for _ in range(size):
        current += rng.choice([1, 1, 2, 3]) if gap else 1
        ids.append(current)
    pos = {event_id: index for index, event_id in enumerate(ids)}
    times = {}
    clock = 0
    for event_id in ids:
        clock += rng.choice([5, 30, 60, 600, 4000])
        times[event_id] = clock
    sources = {}
    for name, (requirement, density) in densities.items():
        sources[name] = (requirement, [i for i in ids if rng.random() < density])
    return ids, pos, times, sources


def probe_paging_engine(seed: int, trials: int) -> dict:
    rng = random.Random(seed)
    failures = []
    stats = Counter()
    shapes = [
        {"a": ("A", 0.3)},
        {"a": ("A", 0.4), "b": ("B", 0.05)},
        {"a": ("A", 0.5), "b": ("B", 0.5)},
        {"a": ("A", 0.2), "b1": ("B", 0.02), "b2": ("B", 0.02), "c": ("C", 0.1)},
        {"a": ("A", 0.0), "b": ("B", 0.3)},
    ]
    for trial in range(trials):
        shape = rng.choice(shapes)
        size = rng.choice([0, 1, 5, 40, 200])
        dataset = sim_dataset(rng, size, shape)
        requirements = tuple(sorted({req for req, _ in shape.values()}))
        max_messages = rng.choice([1, 3, 10])
        max_seconds = rng.choice([600, 3600, 10**9])
        ids, pos, times, sources = dataset
        all_hits: dict[int, set] = {}
        for requirement, source_ids in sources.values():
            for i in source_ids:
                all_hits.setdefault(i, set()).add(requirement)
        reference = sim_groups(
            {i: frozenset(v) for i, v in all_hits.items()},
            pos,
            times,
            requirements,
            max_messages,
            max_seconds,
        )
        reference.sort(key=lambda g: g[1], reverse=True)
        total_candidates = sum(len(v[1]) for v in sources.values())
        unit_max = 2 + max_messages + 1
        for budget in sorted(
            {1, 2, 3, 7, max(1, total_candidates), total_candidates * unit_max + 50}
        ):
            for limit in (1, 2, 5):
                for direction in ("recent", "oldest"):
                    if direction == "recent":
                        pages, requests, checks = sim_engine(
                            dataset, requirements, budget, limit, max_messages, max_seconds
                        )
                        produced = [g for _, groups in pages for g in groups]
                        expected = reference
                    else:
                        mirror = {i: -i for i in ids}
                        m_ids = sorted(mirror[i] for i in ids)
                        m_pos = {mirror[i]: len(ids) - 1 - pos[i] for i in ids}
                        m_times = {mirror[i]: -times[i] for i in ids}
                        m_sources = {
                            n: (r, sorted(mirror[i] for i in s)) for n, (r, s) in sources.items()
                        }
                        shift = (max(ids) + 1) if ids else 0
                        m_ids = [i + shift for i in m_ids]
                        m_pos = {i + shift: p for i, p in m_pos.items()}
                        m_times = {i + shift: t for i, t in m_times.items()}
                        m_sources = {
                            n: (r, [i + shift for i in s]) for n, (r, s) in m_sources.items()
                        }
                        pages, requests, checks = sim_engine(
                            (m_ids, m_pos, m_times, m_sources),
                            requirements,
                            budget,
                            limit,
                            max_messages,
                            max_seconds,
                        )
                        produced = [
                            (shift - e, shift - s) for _, groups in pages for s, e in groups
                        ]
                        expected = sorted(reference, key=lambda g: g[0])
                    for key in (
                        "pages",
                        "no_progress",
                        "bound_violations",
                        "actual_rows_above_charged",
                        "window_reads_above_nM",
                        "pages_window_exhausted_budget",
                        "pages_sources_above_budget",
                    ):
                        stats[key] += checks[key]
                    stats["max_charged_over_budget"] = max(
                        stats["max_charged_over_budget"], checks["max_charged_over_budget"]
                    )
                    stats["runs"] += 1
                    stats["requests"] += requests
                    stats["empty_pages"] += sum(1 for _, groups in pages if not groups)
                    stats[f"state_{pages[-1][0]}"] += 1
                    if produced != expected or pages[-1][0] != "exhausted":
                        failures.append(
                            {
                                "trial": trial,
                                "budget": budget,
                                "limit": limit,
                                "direction": direction,
                                "expected": len(expected),
                                "produced": len(produced),
                            }
                        )
    return {
        "trials": trials,
        "stats": dict(stats),
        "failures": failures[:10],
        "failure_count": len(failures),
    }


def probe_second_draft_no_progress() -> dict:
    """Section 6.7 of the second draft: C=4, budget=1, one source reads id 3."""

    source = [3, 2, 1]
    upper, history = 4, []
    for _ in range(3):
        read = [i for i in source if i < upper][:1]
        lower = read[-1] if len(read) == 1 else 0
        emitted_zone = [i for i in source if lower < i < upper]
        history.append({"C": upper, "read": read, "L": lower, "zone": emitted_zone})
        upper = lower + 1
    return {"iterations": history}


def probe_relevance_revision() -> dict:
    from qq_ai_bot.conversation.projection_compact_schema import PROJECTION_TRIGGERS_0119

    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute(
        "create table canonical_conversations(id text primary key, generation integer default 0,"
        " starts_after_event_id integer default 0, prompt_source_revision integer default 0,"
        " kind text, person_id text, space_id text)"
    )
    db.execute(
        "create table prompt_projections(conversation_id text, payload_json text,"
        " byte_size integer, revision integer default 0, invalidated_reason text)"
    )
    db.execute(
        "create table chat_events(id integer primary key, canonical_conversation_id text,"
        " content text, segments_json text, visual_summary text, external_payload_json text,"
        " suppression_status text, audio_transcript text)"
    )
    for name in (
        "prompt_projection_reset",
        "prompt_projection_event_delete",
        "prompt_projection_event_update",
        "prompt_projection_audio_update",
    ):
        db.execute(PROJECTION_TRIGGERS_0119[name])
    db.execute("insert into canonical_conversations(id) values ('c1')")

    def revision():
        return db.execute(
            "select prompt_source_revision from canonical_conversations where id = 'c1'"
        ).fetchone()[0]

    log = {"initial": revision()}
    db.execute(
        "insert into chat_events(id, canonical_conversation_id, content, audio_transcript)"
        " values (1, 'c1', '', '吉他吉他吉他'), (2, 'c1', '', '吉他吉他吉他吉他吉他吉他')"
    )
    log["after_insert"] = revision()

    def page(after_key):
        rows = db.execute("select id, audio_transcript from chat_events").fetchall()
        ranked = sorted((len(asr), -event_id, event_id) for event_id, asr in rows)
        remaining = [r for r in ranked if after_key is None or r[:2] > after_key]
        return remaining[0] if remaining else None

    first = page(None)
    cursor = {"key": first[:2], "revision": revision()}
    db.execute("update chat_events set audio_transcript = ? where id = 1", ("吉他" * 15,))
    log["after_asr_update"] = revision()
    pages = [first[2]]
    key = cursor["key"]
    while (row := page(key)) is not None:
        pages.append(row[2])
        key = row[:2]
    log["second_draft_pages"] = pages
    log["second_draft_duplicate"] = len(pages) != len(set(pages))
    log["revised_cursor_result"] = (
        "cursor_stale" if cursor["revision"] != revision() else "continue"
    )
    db.execute("delete from chat_events where id = 2")
    log["after_delete"] = revision()
    db.execute("update canonical_conversations set starts_after_event_id = 1 where id = 'c1'")
    log["after_floor_change"] = revision()
    return log


def range_page(
    db,
    terms,
    conv,
    lo,
    hi,
    frozen_frontier=None,
    max_messages=20,
    chunk=2000,
    *,
    budget=None,
    scan_upper=None,
    output_lower=None,
):
    """Discover a ranking range (budget supplied), or replay a fixed range.

    Discovery uses section 6.7's mandatory first merge, checkpoints and (E, S)
    window. Replay reads [lo, hi) to completion using the frozen source plan; its
    output lower bound is already known, so it performs no boundary queries.
    Candidate charging is identical in both modes. Empty exhaustion probes return
    no candidates and do not add boundary work to a replay.
    """

    multi = len(terms) > 1
    unit = 2 + (max_messages + 1 if multi else 0)
    boundary = max_messages + 1 if multi else 0
    db.execute("begin")
    frontier = (
        db.execute("select frontier from state").fetchone()[0]
        if frozen_frontier is None
        else frozen_frontier
    )
    index_lo = max(lo, frontier)
    scan_hi = min(hi, frontier)
    statements = {}
    for name, term in enumerate(terms):
        target = fold_whole(term)
        size = min(3, len(target))
        tag = {1: "u", 2: "b", 3: "t"}[size]
        grams = sorted(
            {hex_token(tag, target[i : i + size]) for i in range(len(target) - size + 1)}
        )
        expr = " AND ".join(f'"{g}"' for g in grams)
        statements[name] = (
            "select id from (select rowid as id from idx where idx match :c"
            " union select rowid from idx where idx match :a"
            " union select event_id from queue where event_id >= :f)"
            " where id in (select id from ev where conv = :conv and kind = 'message')"
            " and id >= :lo and id < :pos order by id desc limit :p",
            {"c": f"{{c_grams}}: ({expr})", "a": f"{{a_grams}}: ({expr})", "f": frontier},
        )
    sources = list(statements)
    if scan_hi > lo:
        sources.append("scan")
    scan_upper = hi if scan_upper is None else scan_upper
    position = dict.fromkeys(sources, scan_upper)
    active = list(sources)
    rows = Counter()
    candidate_reads = Counter()
    charged = steps = 0
    texts: dict[int, tuple] = {}

    def read_source(name, lower, upper, count):
        if name == "scan":
            got = db.execute(
                "select id, content, asr from ev where conv = ? and kind = 'message'"
                " and id >= ? and id < ? and id < ? order by id desc limit ?",
                (conv, max(lo, lower), scan_hi, upper, count),
            ).fetchall()
            for event_id, content, asr in got:
                texts[event_id] = (content, asr)
            rows["scan"] += len(got)
            ids = [r[0] for r in got]
        else:
            sql, params = statements[name]
            ids = [
                r[0]
                for r in db.execute(
                    sql,
                    {
                        **params,
                        "conv": conv,
                        "lo": max(index_lo, lower),
                        "pos": upper,
                        "p": count,
                    },
                )
            ]
            rows["source"] += len(ids)
            for event_id in ids:
                if event_id not in texts:
                    texts[event_id] = db.execute(
                        "select content, asr from ev where id = ?", (event_id,)
                    ).fetchone()
                    rows["verify"] += 1
        candidate_reads[str(name)] += len(ids)
        return ids

    if scan_upper < hi:
        for name in sources:
            charged += unit * len(read_source(name, scan_upper, hi, -1))
    window_candidates = rows["source"] + rows["scan"]
    read_frontier = lo
    state = "exhausted"
    while active:
        page = chunk
        if budget is not None:
            fit = (budget - charged - boundary) // (len(active) * unit)
            page = max(1, min(chunk, fit)) if steps == 0 else min(chunk, fit)
        steps += 1
        for name in list(active):
            ids = read_source(name, lo, position[name], page)
            charged += unit * len(ids)
            if ids:
                position[name] = ids[-1]
            if len(ids) < page:
                active.remove(name)
        read_frontier = max((position[n] for n in active), default=lo)
        if budget is not None:
            charged += boundary
            following = (
                db.execute(
                    "select id from ev where conv = ? and kind = 'message' and id >= ?"
                    " and id < ? order by id limit ?",
                    (conv, read_frontier, hi, boundary),
                ).fetchall()
                if boundary and active
                else []
            )
            rows["boundary"] += len(following)
            if not active:
                output_lower = lo
            elif not multi:
                output_lower = read_frontier
            elif len(following) == boundary:
                output_lower = following[-1][0]
            else:
                output_lower = hi
            if active and budget - charged < len(active) * unit + boundary:
                state = "incomplete"
                break
    output_lower = lo if output_lower is None else output_lower
    hits = {}
    for event_id, row in texts.items():
        if event_id < read_frontier:
            continue
        covers = frozenset(
            t for t in terms if row and any(fold_whole(t) in fold_whole(f or "") for f in row)
        )
        if covers:
            hits[event_id] = covers
    # Enumerate covering windows in hit order, then measure their real message
    # distance by bounded SQL. No full-conversation id scan is needed for pos().
    covers = sim_groups(
        hits,
        {event_id: index for index, event_id in enumerate(sorted(hits))},
        dict.fromkeys(hits, 0),
        tuple(terms),
        len(hits),
        10**9,
    )
    groups = []
    ranking_keys = {}
    for left, right in covers:
        distance = 0
        if left != right:
            distance = len(
                db.execute(
                    "select id from ev where conv = ? and kind = 'message'"
                    " and id > ? and id <= ? order by id limit ?",
                    (conv, left, right, max_messages + 1),
                ).fetchall()
            )
            rows["distance"] += distance
        if distance <= max_messages and right >= output_lower:
            groups.append((left, right))
            required = {left, right}
            for term in set(terms) - hits[left] - hits[right]:
                required.add(min(i for i in hits if left < i < right and term in hits[i]))
            length = sum(
                len(fold_whole(field or ""))
                for event_id in required
                for field in texts[event_id]
                if any(fold_whole(t) in fold_whole(field or "") for t in terms)
            )
            ranking_keys[left, right] = (distance, 0, length, -right, -left)
    db.execute("rollback")
    candidates = rows["source"] + rows["scan"]
    return {
        "frontier_used": frontier,
        "index_source_rows": rows["source"],
        "verify_rows": rows["verify"],
        "scan_rows": rows["scan"],
        "total_rows": rows["source"] + rows["verify"] + rows["scan"],
        "boundary_rows": rows["boundary"],
        "distance_rows": rows["distance"],
        "search_rows": sum(rows.values()),
        "actual_rows_le_charged": sum(rows.values()) <= charged,
        "distance_rows_within_reservation": rows["distance"]
        <= (max_messages + 1 if multi else 0) * candidates,
        "merge_steps": steps,
        "boundary_steps": steps if budget is not None else 0,
        "candidates": candidates,
        "candidate_reads": dict(candidate_reads),
        "window_candidates": window_candidates,
        "unit_u": unit,
        "boundary_b": boundary,
        "charged": charged,
        "result_state": state,
        "range_from": read_frontier,
        "range_to": hi,
        "output_lower": output_lower,
        "groups": groups,
        "ranked_groups": sorted(groups, key=ranking_keys.__getitem__),
        "ranked_keys": sorted(ranking_keys.values()),
    }


def _recompute_states(make_db, terms, lo, hi, chunk) -> dict:
    """First page, then backfill enqueue, then indexing; current vs frozen sources."""

    db = make_db()
    first = range_page(db, terms, 1, lo, hi, chunk=chunk, budget=20000)
    assert first["result_state"] == "exhausted" and first["actual_rows_le_charged"], first
    w, frozen = first["charged"], first["frontier_used"]

    def measured(result):
        return {k: v for k, v in result.items() if k not in {"ranked_groups", "ranked_keys"}}

    out = {"first_page": measured(first), "W": w}

    def record(label):
        current = range_page(db, terms, 1, lo, hi, chunk=chunk)
        replay = range_page(db, terms, 1, lo, hi, frozen, chunk=chunk)
        assert replay["charged"] <= w and replay["actual_rows_le_charged"], replay
        assert replay["groups"] == first["groups"] and current["groups"] == first["groups"]
        assert replay["boundary_rows"] == replay["boundary_steps"] == 0, replay
        out[label] = {
            "current_sources": measured(current),
            "frozen_frontier": measured(replay),
            "frozen_charged_le_W": replay["charged"] <= w,
            "current_charged_le_W": current["charged"] <= w,
            "frozen_groups_equal_first_page": replay["groups"] == first["groups"],
            "current_groups_equal_first_page": current["groups"] == first["groups"],
        }

    while backfill_round(db, 1000):
        pass
    record("after_backfill_enqueue")
    while indexer_round(db, 1000):
        pass
    record("after_indexing")
    out["epoch_unchanged"] = db.execute("select epoch from state").fetchone()[0] == 0
    return out


def probe_relevance_recompute_cost() -> dict:
    """Review counterexample plus a cross-message grouping case (M = 20, u = 23)."""

    terms = ("alpha", "bravo")
    out = {
        "same_message_20": _recompute_states(
            lambda: toy_db(rows=[(i, 1, "message", "alpha bravo", None) for i in range(1, 21)]),
            terms,
            1,
            21,
            chunk=2000,
        )
    }

    def mixed_db():
        db = toy_db(rows=[(i, 1, "message", "alpha bravo", None) for i in range(1, 11)])
        db.executemany(
            "insert into ev values(?,?,?,?,?)",
            [(i, 1, "message", "alpha bravo" if i % 2 else "alpha", None) for i in range(11, 21)],
        )
        return db

    out["mixed_range"] = _recompute_states(mixed_db, terms, 1, 21, chunk=2000)

    def cross_db():
        # 60 messages: alpha every 7th, bravo every 11th, the rest filler; old half
        # below the frontier, newer half still queued on the first page.
        def text(i):
            return "alpha 吉他" if i % 7 == 0 else "bravo 三千" if i % 11 == 0 else f"闲聊{i}"

        db = toy_db(rows=[(i, 1, "message", text(i), None) for i in range(1, 31)])
        db.executemany(
            "insert into ev values(?,?,?,?,?)",
            [(i, 1, "message", text(i), None) for i in range(31, 61)],
        )
        return db

    for chunk in (2000, 3):
        out[f"cross_message_chunk_{chunk}"] = _recompute_states(cross_db, terms, 1, 61, chunk=chunk)
    out["budgeted_ranges"] = probe_relevance_budget_paging()
    return out


def probe_relevance_budget_paging(seed=448, trials=48) -> dict:
    """Budget-stopped range discovery, frozen replays and cross-range continuation.

    The raw-ledger oracle is prepared from fixtures outside the measured reads.
    Actual pages use keyset sources and bounded distance SQL in range_page().
    """

    rng = random.Random(seed)
    stats = Counter()

    def check_pages(db, fixture, terms, lo, hi, budget, chunk, limit, max_messages, progress):
        ledger = [
            row for row in fixture if row[1] == 1 and row[2] == "message" and lo <= row[0] < hi
        ]
        pos = {row[0]: i for i, row in enumerate(ledger)}
        oracle_hits = {
            row[0]: frozenset(
                t for t in terms if any(fold_whole(t) in fold_whole(f or "") for f in row[3:])
            )
            for row in ledger
        }
        expected = sim_groups(
            {i: covers for i, covers in oracle_hits.items() if covers},
            pos,
            dict.fromkeys(pos, 0),
            terms,
            max_messages,
            10**9,
        )
        emitted = []
        emit_upper = scan_upper = hi
        stats["runs"] += 1
        while True:
            first = range_page(
                db,
                terms,
                1,
                lo,
                emit_upper,
                max_messages=max_messages,
                chunk=chunk,
                budget=budget,
                scan_upper=scan_upper,
            )
            stats["ranking_ranges"] += 1
            stats["scanned_ranges"] += first["result_state"] == "incomplete"
            stats["window_ranges"] += first["window_candidates"] > 0
            n = len(first["candidate_reads"])
            margin = max_messages if len(terms) > 1 else 0
            bound = max(
                budget,
                first["unit_u"] * n * margin + n * first["unit_u"] + first["boundary_b"],
            )
            assert first["charged"] <= bound, first
            assert first["actual_rows_le_charged"], first
            assert first["distance_rows_within_reservation"], first
            assert first["window_candidates"] <= n * margin, first
            ranked = first["ranked_groups"]
            page = ranked[:limit]
            emitted.extend(page)
            last_key = first["ranked_keys"][len(page) - 1] if page else None
            # One replay also checks empty and already-drained ranges. With results
            # remaining, continue with the last ranking key until all are emitted.
            while True:
                if progress:
                    backfill_round(db, 7)
                    indexer_round(db, 3)
                replay = range_page(
                    db,
                    terms,
                    1,
                    first["range_from"],
                    emit_upper,
                    first["frontier_used"],
                    max_messages,
                    chunk=chunk + 1,
                    output_lower=first["output_lower"],
                )
                stats["replays"] += 1
                assert replay["charged"] <= first["charged"], (first, replay)
                assert replay["actual_rows_le_charged"], replay
                assert replay["distance_rows_within_reservation"], replay
                assert replay["boundary_rows"] == replay["boundary_steps"] == 0, replay
                assert replay["ranked_groups"] == ranked, (first, replay)
                assert replay["ranked_keys"] == first["ranked_keys"], (first, replay)
                assert all(
                    count <= first["candidate_reads"].get(name, 0)
                    for name, count in replay["candidate_reads"].items()
                ), (first, replay)
                remaining = [
                    (key, group)
                    for key, group in zip(
                        replay["ranked_keys"], replay["ranked_groups"], strict=True
                    )
                    if last_key is None or key > last_key
                ]
                page = remaining[:limit]
                if not page:
                    break
                emitted.extend(group for _, group in page)
                last_key = page[-1][0]
            if first["result_state"] == "exhausted":
                break
            next_cursor = (first["output_lower"], first["range_from"])
            assert next_cursor < (emit_upper, scan_upper), (first, emit_upper, scan_upper)
            emit_upper, scan_upper = next_cursor
        assert len(emitted) == len(set(emitted)), emitted
        assert set(emitted) == set(expected), (emitted, expected)
        stats["groups_emitted"] += len(emitted)
        db.close()

    fixture = [(i, 1, "message", "alpha bravo", None) for i in range(1, 61)]
    db = toy_db(rows=fixture)
    backfill_round(db, 1000)
    indexer_round(db, 1000)
    first = range_page(db, ("alpha", "bravo"), 1, 1, 61, chunk=25, budget=1171)
    replay = range_page(
        db,
        ("alpha", "bravo"),
        1,
        first["range_from"],
        61,
        first["frontier_used"],
        chunk=25,
        output_lower=first["output_lower"],
    )
    assert first["charged"] == 1171 and first["range_from"] == 36, first
    assert len(first["groups"]) == 5 and replay["groups"] == first["groups"], (first, replay)
    assert replay["charged"] == 1150 and replay["merge_steps"] == 2, replay
    counterexample = {
        "budget": 1171,
        "chunk": 25,
        "first_page": {k: v for k, v in first.items() if k != "ranked_keys"},
        "replay": {k: v for k, v in replay.items() if k != "ranked_keys"},
        "previous_replay_formula": replay["charged"] + replay["boundary_b"] * replay["merge_steps"],
    }
    check_pages(db, fixture, ("alpha", "bravo"), 1, 61, 1171, 25, 1, 20, False)

    # Exact chunk multiples, empty hits, sparse ids, short terms, multiple fields,
    # scope boundaries and all three candidate states. Progress changes only the
    # index; the ledger and its revision stay unchanged between discovery/replay.
    for trial in range(trials):
        terms = rng.choice([("甲",), ("alpha", "bravo"), ("alpha", "bravo", "charlie")])
        max_messages = rng.choice([1, 3, 20])
        fixture = []
        event_id = 0
        for i in range(rng.choice([5, 25, 60])):
            event_id += rng.choice([1, 1, 3])
            words = [t for t in terms if rng.random() < 0.35]
            content = " ".join(words) or "闲聊"
            asr = None
            if i % 3 == 0:
                content, asr = "語音", content
            fixture.append((event_id, 2 if i % 13 == 0 else 1, "message", content, asr))
        mode = trial % 4
        for budget, chunk in itertools.product((1, 67, 1171), (1, 3, 25)):
            if mode == 0:
                db = toy_db(rows=fixture)
            elif mode == 1:
                db = toy_db(rows=fixture, init_before_rows=True)
            elif mode == 2:
                db = toy_db(rows=fixture)
                backfill_round(db, 1000)
                indexer_round(db, 1000)
            else:
                split = len(fixture) // 2
                db = toy_db(rows=fixture[:split])
                db.executemany("insert into ev values(?,?,?,?,?)", fixture[split:])
            lo = fixture[1][0]
            hi = fixture[-1][0]  # intentionally excludes the last event
            check_pages(
                db,
                fixture,
                terms,
                lo,
                hi,
                budget,
                chunk,
                rng.choice([1, 2, 5]),
                max_messages,
                progress=mode != 2,
            )
    return {
        "seed": seed,
        "datasets": trials + 1,
        "counterexample": counterexample,
        "stats": dict(stats),
        "invariant_failures": 0,
    }


def probe_plugin(seed: int) -> dict:
    rng = random.Random(seed)
    dataset = sim_dataset(rng, 400, {"a": ("A", 0.0)}, gap=False)
    ids, pos, times, sources = dataset
    hits = [ids[5], ids[9], ids[12]]  # old messages, far below the first batch
    sources = {"a": ("A", hits)}
    dataset = (ids, pos, times, sources)
    pages, requests, checks = sim_engine(
        dataset, ("A",), budget=2, limit=3, max_messages=0, max_seconds=0
    )
    first_page = [e for _, groups in pages[:1] for _, e in groups]
    looped = [e for _, groups in pages for _, e in groups][:3]
    return {
        "hits": hits,
        "second_draft_single_request": sorted(first_page),
        "revised_internal_continuation": sorted(looped),
        "requests_used": requests,
        "no_progress_requests": checks["no_progress"],
        "bound_violations": checks["bound_violations"],
        "final_state": pages[-1][0],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=200_000)
    parser.add_argument("--queries", type=int, default=400)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--enqueue-rows", type=int, default=1_000_000)
    parser.add_argument("--normalization-samples", type=int, default=200_000)
    parser.add_argument("--paging-trials", type=int, default=300)
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
        "normalization": probe_normalization(seed=15, samples=args.normalization_samples),
        "queue": probe_queue(),
        "snapshot": probe_snapshot(args.workdir),
        "lifecycle": probe_lifecycle(seed=6),
        "paging_engine": probe_paging_engine(seed=12, trials=args.paging_trials),
        "second_draft_scan_no_progress": probe_second_draft_no_progress(),
        "relevance_revision": probe_relevance_revision(),
        "relevance_recompute_cost": probe_relevance_recompute_cost(),
        "plugin": probe_plugin(seed=4),
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
