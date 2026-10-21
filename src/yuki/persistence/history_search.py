"""Literal history matching and conversation-scoped ASCII FTS terms.

The index deliberately supplies a superset. Only original content and each
current ASR segment independently establish a literal match.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal
from unicodedata import normalize

from opencc import OpenCC

from yuki.domain.audio import parse_transcripts

MatchMode = Literal["all", "any", "phrase"]


@lru_cache(maxsize=1)
def _converter() -> OpenCC:
    return OpenCC("t2s")


def fold_text(value: str) -> str:
    """Normalize an entire independent field, preserving word boundaries."""
    return " ".join(normalize("NFKC", normalize("NFKC", value).casefold()).split())


def search_fields(content: str, audio_transcript: str = "") -> tuple[tuple[str, str], ...]:
    return (
        ("content", content),
        *(
            (f"audio_transcript:{item.segment_index}", item.text)
            for item in parse_transcripts(audio_transcript)
            if item.source == "current"
        ),
    )


@dataclass(frozen=True, slots=True)
class LiteralTerm:
    base: str
    simplified: str


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    terms: tuple[LiteralTerm, ...]
    exclude: tuple[LiteralTerm, ...]
    match: MatchMode


def _literal(value: str) -> LiteralTerm:
    return LiteralTerm(value, _converter().convert(value))


def prepare_query(
    keyword: str = "", match: MatchMode = "all", exclude: tuple[str, ...] = ()
) -> HistoryQuery:
    if match not in ("all", "any", "phrase"):
        raise ValueError("match must be all, any, or phrase")
    folded = fold_text(keyword)
    words = (folded,) if match == "phrase" and folded else tuple(folded.split())
    return HistoryQuery(
        tuple(_literal(word) for word in dict.fromkeys(words)),
        tuple(_literal(word) for item in exclude if (word := fold_text(item))),
        match,
    )


def _gram(value: str) -> str:
    return f"g{len(value)}x{value.encode('utf-8').hex()}"


def prepare_terms(content: str, audio_transcript: str = "") -> tuple[str, ...]:
    """Prepare unscoped 1/2/3-grams outside a writer transaction."""
    terms: set[str] = set()
    for _, raw in search_fields(content, audio_transcript):
        base = fold_text(raw)
        for value in {base, _converter().convert(base)}:
            for width in (1, 2, 3):
                terms.update(
                    _gram(value[index : index + width]) for index in range(len(value) - width + 1)
                )
    return tuple(sorted(terms))


def _prefix(conversation_id: str) -> str:
    return "c" + conversation_id.replace("-", "")


def _person_token(person_id: str) -> str:
    return "p" + person_id.replace("-", "")


def _author_token(author_kind: str) -> str:
    return "a" + author_kind.encode("utf-8").hex()


def namespace_terms(
    conversation_id: str,
    terms: tuple[str, ...],
    *,
    author_person_id: str | None = None,
    author_kind: str | None = None,
) -> str:
    prefix = _prefix(conversation_id)
    identities = (
        *((_person_token(author_person_id),) if author_person_id else ()),
        *((_author_token(author_kind),) if author_kind else ()),
    )
    return " ".join(prefix + term for term in (*terms, *identities))


def compile_match(
    conversation_id: str,
    query: HistoryQuery,
    *,
    person_ids: tuple[str, ...] = (),
    author_kind: str | None = None,
) -> str | None:
    """One MATCH expression; exclusions require exact post-verification."""
    prefix = _prefix(conversation_id)
    expressions: list[str] = []
    for term in query.terms:
        branches: list[str] = []
        for value in dict.fromkeys((term.base, term.simplified)):
            width = min(3, len(value))
            grams = dict.fromkeys(
                value[index : index + width] for index in range(len(value) - width + 1)
            )
            branches.append(
                "(" + " AND ".join('"' + prefix + _gram(gram) + '"' for gram in grams) + ")"
            )
        expressions.append("(" + " OR ".join(branches) + ")")
    separator = " OR " if query.match == "any" else " AND "
    if not expressions:
        return None
    selected = ["(" + separator.join(expressions) + ")"]
    if person_ids:
        selected.append(
            "("
            + " OR ".join('"' + prefix + _person_token(person_id) + '"' for person_id in person_ids)
            + ")"
        )
    if author_kind:
        selected.append('"' + prefix + _author_token(author_kind) + '"')
    return " AND ".join(selected)


def match_fields(
    query: HistoryQuery, content: str, audio_transcript: str = ""
) -> tuple[str, ...] | None:
    """Return matching original fields, or None when exact conditions fail."""
    fields = tuple(
        (label, fold_text(raw)) for label, raw in search_fields(content, audio_transcript)
    )
    representations = tuple((label, base, _converter().convert(base)) for label, base in fields)

    def matching(term: LiteralTerm) -> tuple[str, ...]:
        return tuple(
            label
            for label, base, simplified in representations
            if term.base in base or term.simplified in simplified
        )

    if any(matching(term) for term in query.exclude):
        return None
    matched = tuple(matching(term) for term in query.terms)
    if matched and not (any(matched) if query.match == "any" else all(matched)):
        return None
    return tuple(dict.fromkeys(label for labels in matched for label in labels))
