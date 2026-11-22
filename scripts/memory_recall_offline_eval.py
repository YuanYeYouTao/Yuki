"""Offline evaluation of search_memory ranking on a production database (记忆意图召回评估 §1, §4).

Read-only (``mode=ro`` and ``query_only``), streamed and bounded; prints aggregate numbers
only, never fact text, names or query text. It builds the query sets from the database
itself (rare two/three-character words of sampled facts, subject name plus word, and the
last 14 days of real ``search_memory`` queries with replies as silver labels), embeds every
query once with the configured embedding provider, and compares the old pipeline
(replicated here: trigram FTS + ID-ordered LIKE, cosine bands before RRF) with the
current one (``yuki.memory.lexical`` terms and pool scoring, weighted scan, plain RRF).

Run inside the bot container::

    docker exec -i <bot> /app/.venv/bin/python - < scripts/memory_recall_offline_eval.py

On an image older than ``yuki.memory.lexical``, copy ``src/yuki/memory/lexical.py`` into
the container and pass its path as ``YUKI_LEXICAL_PATH``.
"""

from __future__ import annotations

import array
import asyncio
import collections
import datetime as dt
import gzip
import importlib.util
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata

import yuki.persistence.vec1 as v1
from yuki.config import Settings
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.query import normalize_query_text as norm

if os.environ.get("YUKI_LEXICAL_PATH"):
    # An image older than this module: load the copied source instead.
    _spec = importlib.util.spec_from_file_location("yuki_lexical", os.environ["YUKI_LEXICAL_PATH"])
    _lexical = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_lexical)
else:
    import yuki.memory.lexical as _lexical
LexicalDocument = _lexical.LexicalDocument
entity_terms = _lexical.entity_terms
lexical_terms = _lexical.lexical_terms
normalized_names = _lexical.normalized_names
score_documents = _lexical.score_documents

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class OldQuery:
    """The pre-change ``build_safe_lexical_query`` (trigrams to FTS, short terms to LIKE)."""

    def __init__(self, value: str, term_limit: int = 12) -> None:
        normalized = norm(value)
        terms: list[str] = []
        short: list[str] = []
        for raw in _WORD.findall(unicodedata.normalize("NFKC", normalized)):
            token = raw.casefold()
            if len(token) < 3:
                if token not in short:
                    short.append(token)
                if len(terms) + len(short) >= term_limit:
                    break
                continue
            cjk = any("\u3400" <= ch <= "\u9fff" for ch in token)
            generated = (
                tuple(token[i : i + 3] for i in range(len(token) - 2))
                if cjk and len(token) > 3
                else (token,)
            )
            for term in generated:
                if term not in terms:
                    terms.append(term)
                if len(terms) + len(short) >= term_limit:
                    break
            if len(terms) + len(short) >= term_limit:
                break
        self.normalized_text = normalized
        self.fts_expression = " OR ".join(f'"{term}"' for term in terms)
        self.like_terms = tuple(short)


def bigrams(text):
    compact = "".join(norm(text).split())
    return frozenset(compact[i : i + 2] for i in range(len(compact) - 1))


def similarity(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def demote_duplicates(ordered, texts, *, threshold):
    grams = {f: bigrams(texts[f]) for f in ordered}
    kept, repeated = [], []
    for f in ordered:
        target = (
            repeated if any(similarity(grams[f], grams[o]) >= threshold for o in kept) else kept
        )
        target.append(f)
    return (*kept, *repeated)


DB = "file:/app/data/yuki.db?mode=ro"
c = sqlite3.connect(DB, uri=True)
c.execute("PRAGMA query_only=1")
c.enable_load_extension(True)
c.load_extension(str(v1.extension_path()))
c.enable_load_extension(False)
NOW = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
ACTIVE = (
    "mf.status='active' AND mf.review_state!='quarantined' "
    "AND (mf.valid_until IS NULL OR mf.valid_until > :now)"
)
K = 60
LIMIT = 4

# ---------- facts (small: ~1.6k rows) ----------
facts = {}
for row in c.execute(
    f"""
    SELECT mf.id, mf.scope_type, mf.kind, mf.memory_key, mf.category, mf.normalized_content,
           coalesce(mf.title,''), mf.canonical_subject_person_id, mf.canonical_subject_space_id,
           mf.importance, mf.confidence, mf.updated_at, mf.conflict_state, mf.content
    FROM memory_facts mf WHERE {ACTIVE}""",
    {"now": NOW},
):
    facts[row[0]] = dict(
        zip(
            (
                "id",
                "scope",
                "kind",
                "key",
                "category",
                "ncontent",
                "title",
                "person",
                "space",
                "importance",
                "confidence",
                "updated",
                "conflict",
                "content",
            ),
            row,
            strict=True,
        )
    )
persons = {
    pid: (root or pid, name or "")
    for pid, root, name in c.execute("SELECT id, merged_into_person_id, display_name FROM persons")
}
family = collections.defaultdict(set)
for pid, (root, _) in persons.items():
    family[root].add(pid)
aliases = collections.defaultdict(list)
for pid, alias in c.execute("SELECT canonical_person_id, alias FROM person_aliases"):
    aliases[persons.get(pid, (pid, ""))[0]].append(alias)
spaces = dict(c.execute("SELECT id, name FROM spaces"))


def entity_of(f):
    names = []
    if f["person"]:
        root = persons.get(f["person"], (f["person"], ""))[0]
        names += [persons[p][1] for p in family[root] if p in persons] + aliases[root]
    if f["space"]:
        names.append(spaces.get(f["space"], ""))
    return normalized_names(names)


docs = {
    fid: LexicalDocument(
        fid,
        entity_of(f),
        norm(f["title"]),
        norm(f["key"] + " " + f["category"]),
        norm(f["ncontent"]),
    )
    for fid, f in facts.items()
}

# ---------- vectors of active facts (float32 arrays, ~6MB) ----------
vectors = {}
for fid, blob in c.execute("SELECT fact_id, vector FROM memory_vectors_1"):
    if fid in facts:
        vectors[fid] = array.array("f", blob)
print("facts", len(facts), "vectors", len(vectors), file=sys.stderr)

# ---------- embedding provider (real query embeddings, cached per text) ----------
provider = MemoryEmbeddingRuntime._build_provider(Settings())
emb_cache = {}


async def embed_all(texts):
    sem = asyncio.Semaphore(2)

    async def one(t):
        async with sem:
            r = await provider.embed_query(t)
            emb_cache[t] = array.array("f", r.vectors[0].values)

    await asyncio.gather(*(one(t) for t in texts if t not in emb_cache))


def semantic(q):
    qv = emb_cache[q]
    sims = []
    for fid, v in vectors.items():
        sims.append((sum(a * b for a, b in zip(qv, v, strict=True)), fid))
    sims.sort(key=lambda x: (-x[0], x[1]))
    return {fid: s for s, fid in sims[:50]}


# ---------- old lexical ----------
def old_lexical(q):
    safe = OldQuery(q)
    rows = []
    t0 = time.perf_counter()
    if safe.fts_expression:
        rows += c.execute(
            f"""SELECT mf.id, bm25(memory_facts_fts,1.0,4.0,2.0) r FROM memory_facts_fts
            JOIN memory_facts mf ON mf.id=memory_facts_fts.rowid
            WHERE memory_facts_fts MATCH :m AND {ACTIVE} ORDER BY r, mf.id LIMIT 51""",
            {"m": safe.fts_expression, "now": NOW},
        ).fetchall()
    if safe.like_terms:
        params = {"now": NOW}
        ors = []
        for i, t in enumerate(safe.like_terms):
            params[f"t{i}"] = (
                "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            ors += [
                f"mf.normalized_content LIKE :t{i} ESCAPE '\\'",
                f"mf.memory_key LIKE :t{i} ESCAPE '\\'",
                f"mf.category LIKE :t{i} ESCAPE '\\'",
            ]
        rows += c.execute(
            f"SELECT mf.id, 1000.0 FROM memory_facts mf WHERE {ACTIVE} AND ({' OR '.join(ors)}) "
            "ORDER BY mf.id LIMIT 51",
            params,
        ).fetchall()
    elapsed = time.perf_counter() - t0
    seen = {}
    for fid, r in rows:
        seen.setdefault(fid, r)
    cand = dict(list(seen.items())[:50])
    nq = safe.normalized_text
    return cand, elapsed, nq, None


def old_rank(q, lex, sem):
    cand, _, nq, _ = lex
    pool = set(cand) | set(sem)

    def exact_reason(fid):
        f = facts[fid]
        if fid not in cand:
            return None
        if norm(f["key"]) == nq:
            return "memory_key_exact"
        if norm(f["ncontent"]) == nq:
            return "content_exact"
        return None

    lr = {fid: i for i, fid in enumerate(sorted(cand, key=lambda x: (cand[x], x)), 1)}
    sr = {fid: i for i, fid in enumerate(sorted(sem, key=lambda x: (-sem[x], x)), 1)}

    def fusion(fid):
        return (1 / (K + lr[fid]) if fid in lr else 0) + (1 / (K + sr[fid]) if fid in sr else 0)

    def band(fid):
        if exact_reason(fid):
            return 21
        return int(max(0.0, sem.get(fid, 0.0)) * 20)

    return (
        sorted(pool, key=lambda x: (-band(x), -fusion(x), -facts[x]["importance"], x)),
        fusion,
        band,
    )


# ---------- new lexical ----------
def entity_ids(terms):
    wanted = entity_terms(terms)
    if not wanted:
        return set()
    roots = set()
    for root, name in persons.values():
        if any(t in norm(name) for t in wanted):
            roots.add(root)
    for root, names in aliases.items():
        if any(t in norm(a) for a in names for t in wanted):
            roots.add(root)
    return {p for r in roots for p in family[r]}


def new_lexical(q):
    terms = lexical_terms(q, term_limit=12)
    t0 = time.perf_counter()
    rows = []
    if terms.index_terms:
        rows += c.execute(
            "SELECT mf.id FROM memory_facts_fts"
            " JOIN memory_facts mf ON mf.id=memory_facts_fts.rowid"
            f" WHERE memory_facts_fts MATCH :m AND {ACTIVE}"
            " ORDER BY bm25(memory_facts_fts,1.0,4.0,2.0), mf.id LIMIT 51",
            {"m": terms.fts_expression, "now": NOW},
        ).fetchall()
    ents = entity_ids(terms)
    if terms.scan_terms or ents:
        params = {"now": NOW}
        parts = []
        for i, t in enumerate(terms.scan_terms):
            params[f"t{i}"] = (
                "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            parts.append(
                " OR ".join(
                    f"mf.{col} LIKE :t{i} ESCAPE '\\'"
                    for col in ("normalized_content", "memory_key", "category", "title")
                )
            )
        score = (
            " + ".join(
                f"CASE WHEN {p} THEN {len(t)} ELSE 0 END"
                for p, t in zip(parts, terms.scan_terms, strict=True)
            )
            or "0"
        )
        if ents:
            ph = ",".join(f":e{i}" for i in range(len(ents)))
            params.update({f"e{i}": e for i, e in enumerate(ents)})
            score += f" + (mf.canonical_subject_person_id IN ({ph}))*2"
        rows += c.execute(
            f"SELECT id FROM (SELECT mf.id, mf.importance, ({score}) s"
            f" FROM memory_facts mf WHERE {ACTIVE}) "
            "WHERE s > 0 ORDER BY s DESC, importance DESC, id DESC LIMIT 51",
            params,
        ).fetchall()
    elapsed = time.perf_counter() - t0
    return terms, set(r[0] for r in rows), elapsed


def new_rank(q, lex, sem, *, band_on, dedupe):
    terms, cand, _ = lex
    pool = sorted(set(cand) | set(sem))
    scores = score_documents(terms, [docs[f] for f in pool])
    nq = terms.normalized

    def exact(fid):
        f = facts[fid]
        return norm(f["key"]) == nq or norm(f["ncontent"]) == nq

    lexical = [f for f in pool if scores[f] > 0]
    lr = {
        fid: i
        for i, fid in enumerate(
            sorted(
                lexical,
                key=lambda x: (
                    not exact(x),
                    -scores[x],
                    facts[x]["conflict"] == "contested",
                    -facts[x]["importance"],
                    -facts[x]["confidence"],
                    x,
                ),
            ),
            1,
        )
    }
    sr = {fid: i for i, fid in enumerate(sorted(sem, key=lambda x: (-sem[x], x)), 1)}

    def fusion(fid):
        return (1 / (K + lr[fid]) if fid in lr else 0) + (1 / (K + sr[fid]) if fid in sr else 0)

    def band(fid):
        if exact(fid) and fid in lr:
            return 21
        return int(max(0.0, sem.get(fid, 0.0)) * 20) if band_on else 0

    ordered = [
        f
        for f in sorted(pool, key=lambda x: (-band(x), -fusion(x), -facts[x]["importance"], x))
        if f in lr or f in sr
    ]
    if dedupe:
        ordered = list(
            demote_duplicates(ordered, {f: facts[f]["content"] for f in ordered}, threshold=0.6)
        )
    return ordered, scores


# ---------- query sets ----------
rng = random.Random(7)
CJKRE = re.compile(r"[一-鿿]+")
df2 = collections.Counter()
for f in facts.values():
    text = f["ncontent"]
    df2.update({text[i : i + 2] for i in range(len(text) - 1) if CJKRE.fullmatch(text[i : i + 2])})


def rare_word(f, n):
    text = f["ncontent"]
    grams = [text[i : i + n] for i in range(len(text) - n + 1) if CJKRE.fullmatch(text[i : i + n])]
    grams = [
        g
        for g in grams
        if not any(g in (persons.get(p, ("", ""))[1] or "") for p in family.get(f["person"], ()))
    ]
    if not grams:
        return None

    def score(g):
        return sum(df2[g[i : i + 2]] for i in range(len(g) - 1))

    return min(grams, key=lambda g: (score(g), g))


def name_of(f):
    if not f["person"]:
        return None
    root = persons.get(f["person"], (f["person"], ""))[0]
    name = persons.get(root, (root, ""))[1]
    return name if name and len(name) <= 8 else None


pool_person = [
    f for f in facts.values() if f["scope"] in ("person", "person_group") and f["kind"] != "episode"
]
pool_group = [f for f in facts.values() if f["scope"] == "group"]
pool_self = [f for f in facts.values() if f["scope"] == "self"]
sample = (
    rng.sample(sorted(pool_person, key=lambda f: f["id"]), 80)
    + rng.sample(sorted(pool_group, key=lambda f: f["id"]), 20)
    + rng.sample(sorted(pool_self, key=lambda f: f["id"]), 30)
)
queries = []  # (set, query, target ids)
for f in sample:
    w2, w3 = rare_word(f, 2), rare_word(f, 3)
    nm = name_of(f)
    if w3:
        queries.append(("K2 话题词(3字)", w3, {f["id"]}))
    if w2:
        queries.append(("K2 话题词(2字)", w2, {f["id"]}))
    if nm and w2:
        queries.append(("K1 人名+词(空格)", f"{nm} {w2}", {f["id"]}))
        queries.append(("K3 人名+词(连写)", f"{nm}的{w2}", {f["id"]}))

# production queries with silver labels (14 days, stats only)
since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
ops = {}
for tid, op, blob in c.execute(
    "SELECT turn_id, operation_id, payload_gzip FROM execution_trace_entries "
    "WHERE kind='tool_start' AND created_at>=? AND payload_gzip IS NOT NULL",
    (since,),
):
    call = json.loads(gzip.decompress(blob))["data"]["call"]
    if call["function"].get("name") != "search_memory":
        continue
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except Exception:
        continue
    if args.get("query") and "target" not in args:
        ops[op] = {"turn": tid, "q": args["query"]}
for op, blob in c.execute(
    "SELECT operation_id, payload_gzip FROM execution_trace_entries WHERE kind='tool_end' "
    "AND created_at>=? AND payload_gzip IS NOT NULL",
    (since,),
):
    if op in ops:
        try:
            ops[op]["mem"] = json.loads(json.loads(gzip.decompress(blob))["data"]["result"]).get(
                "memories", []
            )
        except Exception:
            pass
replies = collections.defaultdict(str)
turns = {o["turn"] for o in ops.values()}
for tid, blob in c.execute(
    "SELECT turn_id, payload_gzip FROM execution_trace_entries WHERE kind='social_delivery' "
    "AND created_at>=? AND payload_gzip IS NOT NULL",
    (since,),
):
    if tid in turns:
        ev = json.loads(gzip.decompress(blob))["data"].get("event_id")
        row = (
            c.execute("SELECT content FROM chat_events WHERE id=?", (ev,)).fetchone()
            if ev
            else None
        )
        if row and row[0]:
            replies[tid] += row[0]


def cjk_bigrams(x):
    x = re.sub(r"\s+", "", x)
    return {x[i : i + 2] for i in range(len(x) - 1) if CJKRE.fullmatch(x[i : i + 2])}


real = []
for o in ops.values():
    rb = cjk_bigrams(replies[o["turn"]])
    silver = set()
    for m in o.get("mem", []):
        mb = cjk_bigrams(str(m.get("content", "")))
        if mb and len(mb & rb) / len(mb) >= 0.25 and m.get("fact_id") in facts:
            silver.add(m["fact_id"])
    real.append((o["q"], silver))

texts = sorted({q for _, q, _ in queries} | {q for q, _ in real})
print("embedding", len(texts), "queries", file=sys.stderr)
asyncio.run(embed_all(texts))

# ---------- run ----------
variants = ["A 现行", "B 旧词法·去分档", "C 新词法·保留分档", "D 新词法·去分档", "E D+去重"]
stats = {
    v: collections.defaultdict(lambda: [0, 0, 0.0]) for v in variants
}  # set -> [n, hit@4, rr@10]
lat_old, lat_new = [], []
noise = collections.Counter()
dup = collections.Counter()
lexonly = collections.Counter()


def record(v, set_name, ordered, targets):
    s = stats[v][set_name]
    s[0] += 1
    s[1] += any(f in targets for f in ordered[:LIMIT])
    for i, f in enumerate(ordered[:10], 1):
        if f in targets:
            s[2] += 1 / i
            break


def run(set_name, q, targets, real_query):
    sem = semantic(q)
    ol = old_lexical(q)
    nl = new_lexical(q)
    lat_old.append(ol[1])
    lat_new.append(nl[2])
    a, _, _ = old_rank(q, ol, sem)
    # B: old lexical, no band
    cand, _, nq, _ = ol
    lr = {fid: i for i, fid in enumerate(sorted(cand, key=lambda x: (cand[x], x)), 1)}
    sr = {fid: i for i, fid in enumerate(sorted(sem, key=lambda x: (-sem[x], x)), 1)}

    def fus(x):
        return (1 / (K + lr[x]) if x in lr else 0) + (1 / (K + sr[x]) if x in sr else 0)

    def ex(x):
        return x in cand and (norm(facts[x]["key"]) == nq or norm(facts[x]["ncontent"]) == nq)

    b = sorted(set(cand) | set(sem), key=lambda x: (not ex(x), -fus(x), -facts[x]["importance"], x))
    cc, _ = new_rank(q, nl, sem, band_on=True, dedupe=False)
    d, _ = new_rank(q, nl, sem, band_on=False, dedupe=False)
    e, _ = new_rank(q, nl, sem, band_on=False, dedupe=True)
    for v, ordered in zip(variants, (a, b, cc, d, e), strict=True):
        record(v, set_name, ordered, targets)
        if real_query:
            top = ordered[:LIMIT]
            multi = [t for t, _ in lexical_terms(q, term_limit=12).weights if len(t) > 1]
            for f in top:
                hay = docs[f].content + " " + docs[f].key
                if (
                    f not in sem
                    and multi
                    and not any(t in hay or t in docs[f].entity for t in multi)
                ):
                    noise[v] += 1
                if f not in sem:
                    lexonly[v] += 1
            grams = [bigrams(facts[f]["content"]) for f in top]
            dup[v] += sum(
                1
                for i in range(len(top))
                for j in range(i)
                if similarity(grams[i], grams[j]) >= 0.6
            )


for set_name, q, targets in queries:
    run(set_name, q, targets, False)
for q, silver in real:
    if silver:
        run("R 生产查询(银标)", q, silver, True)
    else:
        run("R 生产查询(无标)", q, set(), True)

print("== hit@4 / MRR@10 ==")
for set_name in sorted({s for v in variants for s in stats[v]}):
    line = [set_name]
    for v in variants:
        n, h, rr = stats[v][set_name]
        line.append(f"{v}: {h}/{n} ({100 * h / max(n, 1):.0f}%) mrr {rr / max(n, 1):.2f}")
    print(" | ".join(line))
print("top-4 lexical-only (not in vector top-50) per variant:", dict(lexonly))
print("top-4 lexical-only with no multi-char term match (noise):", dict(noise))
print("top-4 near-duplicate pairs (bigram Jaccard>=0.6):", dict(dup))


def pct(xs, p):
    xs = sorted(xs)
    return 1000 * xs[min(len(xs) - 1, int(p * len(xs)))]


print(
    f"lexical SQL ms old p50 {pct(lat_old, 0.5):.2f} p95 {pct(lat_old, 0.95):.2f}"
    f" | new p50 {pct(lat_new, 0.5):.2f} p95 {pct(lat_new, 0.95):.2f}"
)
print("queries", len(queries), "real", len(real), "silver", sum(1 for _, s in real if s))
