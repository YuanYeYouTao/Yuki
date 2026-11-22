"""Lexical side of ``search_memory``: query terms and per-field scoring.

Contract: docs/architecture/memory-v2-retrieval.md "词法打分". The query is cut into
CJK uni/bi/trigrams and non-CJK words; trigrams and long words go through the existing
FTS5 trigram index, two-character terms through one LIKE scan. Candidates are scored
in Python over the candidate pool only (document frequency counted in the pool, no
resident index or dictionary): each matched term adds its query weight times its IDF
times the weight of the best field it occurs in. When the query has a term longer
than one character, a candidate matching only single characters scores zero and is
not a lexical candidate (it may still arrive through the vector side).

Pure functions; no database access.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from yuki.memory.query import normalize_query_text

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+")

# Query weight by term shape: a longer CJK n-gram says more than a shorter one.
UNIGRAM = 0.18
BIGRAM = 1.0
TRIGRAM = 1.25
WORD = 1.1  # a non-CJK run of at least two characters

# Field weight: the subject's names > episode title > key and category > content.
ENTITY = 1.55
TOPIC = 1.45
KEY = 1.3
CONTENT = 1.0


@dataclass(frozen=True, slots=True)
class LexicalTerms:
    normalized: str
    weights: tuple[tuple[str, float], ...]  # scoring terms in first-occurrence order
    index_terms: tuple[str, ...]  # three or more characters: the FTS5 trigram index
    scan_terms: tuple[str, ...]  # shorter terms: the LIKE scan
    has_word: bool  # some term is longer than one character

    @property
    def fts_expression(self) -> str:
        return " OR ".join(f'"{term}"' for term in self.index_terms)


@dataclass(frozen=True, slots=True)
class LexicalDocument:
    """One candidate's searchable fields, each already normalized."""

    fact_id: int
    entity: str  # the subject person's or space's names
    topic: str  # episode title
    key: str  # memory_key and category
    content: str

    def fields(self) -> tuple[tuple[str, float], ...]:
        return (
            (self.entity, ENTITY),
            (self.topic, TOPIC),
            (self.key, KEY),
            (self.content, CONTENT),
        )


def lexical_terms(value: str, *, term_limit: int) -> LexicalTerms:
    """Cut ``value`` into weighted terms; FTS syntax in the input is never passed on."""
    normalized = normalize_query_text(value)
    weights: dict[str, float] = {}
    index_terms: list[str] = []
    scan_terms: list[str] = []
    unigrams: list[str] = []

    def add(term: str, weight: float) -> None:
        weights[term] = max(weights.get(term, 0.0), weight)

    for token in _TOKEN.findall(normalized):
        cursor = 0
        pieces: list[tuple[str, bool]] = []
        for match in _CJK.finditer(token):
            if match.start() > cursor:
                pieces.append((token[cursor : match.start()], False))
            pieces.append((match.group(), True))
            cursor = match.end()
        if cursor < len(token):
            pieces.append((token[cursor:], False))
        joined: list[tuple[str, bool]] = []
        for piece, cjk in pieces:
            # One letter or digit beside a CJK run belongs to it: "B站", "A股", "U盘".
            if joined and (cjk or len(piece) == 1) and (joined[-1][1] or len(joined[-1][0]) == 1):
                joined[-1] = (joined[-1][0] + piece, True)
            else:
                joined.append((piece, cjk))
        for piece, cjk in joined:
            if not cjk:
                if len(piece) < 2:
                    unigrams.append(piece)
                    continue
                add(piece, WORD)
                (index_terms if len(piece) >= 3 else scan_terms).append(piece)
                continue
            for index in range(len(piece)):
                unigrams.append(piece[index])
                if index + 2 <= len(piece):
                    scan_terms.append(piece[index : index + 2])
                if index + 3 <= len(piece):
                    index_terms.append(piece[index : index + 3])
    index_terms = list(dict.fromkeys(index_terms))[:term_limit]
    scan_terms = list(dict.fromkeys(scan_terms))[:term_limit]
    unigrams = list(dict.fromkeys(unigrams))[:term_limit]
    has_word = bool(index_terms or scan_terms)
    if not has_word:
        scan_terms = unigrams
    for term in (*index_terms, *scan_terms):
        if len(term) == 1:
            add(term, UNIGRAM)
        elif not _CJK.search(term):
            add(term, WORD)
        else:
            add(term, BIGRAM if len(term) == 2 else TRIGRAM)
    for term in unigrams:
        add(term, UNIGRAM)
    return LexicalTerms(
        normalized=normalized,
        weights=tuple(weights.items()),
        index_terms=tuple(index_terms),
        scan_terms=tuple(scan_terms),
        has_word=has_word,
    )


def entity_terms(terms: LexicalTerms) -> tuple[str, ...]:
    """Terms looked up in person and space names: every term of two or more characters."""
    return tuple(term for term, _ in terms.weights if len(term) >= 2)


def score_documents(terms: LexicalTerms, documents: Sequence[LexicalDocument]) -> dict[int, float]:
    """Score every document in the pool; IDF is counted over this pool."""
    count = len(documents)
    matched: dict[int, dict[str, float]] = {}
    frequency: dict[str, int] = {}
    for document in documents:
        fields = document.fields()
        best: dict[str, float] = {}
        for term, _ in terms.weights:
            weight = max((field_weight for text, field_weight in fields if term in text), default=0)
            if weight:
                best[term] = weight
                frequency[term] = frequency.get(term, 0) + 1
        matched[document.fact_id] = best
    scores: dict[int, float] = {}
    for document in documents:
        best = matched[document.fact_id]
        if terms.has_word and not any(len(term) > 1 for term in best):
            scores[document.fact_id] = 0.0
            continue
        scores[document.fact_id] = math.fsum(
            query_weight * math.log(1 + (count + 1) / (frequency[term] + 1)) * best[term]
            for term, query_weight in terms.weights
            if term in best
        )
    return scores


def normalized_names(names: Iterable[str]) -> str:
    return " ".join(dict.fromkeys(normalize_query_text(name) for name in names if name))
