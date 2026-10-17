"""Privacy-bounded, versioned text projections used only for embedding."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from yuki.memory.models import MemoryFact, MemoryQuery

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_PLATFORM_ID = re.compile(r"(?<!\d)\d{5,20}(?!\d)")


def _bounded(value: str, maximum: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL.sub(" ", normalized)
    normalized = _PLATFORM_ID.sub("[id]", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()[:maximum]


class EmbeddingDocumentBuilder:
    """Project only non-identifying fact fields into one stable document template."""

    def __init__(self, *, template_version: int, max_characters: int) -> None:
        if template_version != 1:
            raise ValueError("unsupported embedding document template version")
        if max_characters <= 0:
            raise ValueError("embedding text limit must be positive")
        self.template_version = template_version
        self.max_characters = max_characters

    def build(self, fact: MemoryFact) -> str:
        return self.build_fields(
            kind=fact.kind.value,
            category=fact.category,
            memory_key=fact.memory_key,
            content=fact.content,
        )

    def build_fields(
        self,
        *,
        kind: str,
        category: str,
        memory_key: str,
        content: str,
    ) -> str:
        fields = (
            ("Kind", kind),
            ("Category", category),
            ("Key", memory_key),
            ("Fact", content),
        )
        text = "\n".join(
            f"{label}: {_bounded(value, self.max_characters)}" for label, value in fields
        )
        return text[: self.max_characters]

    def content_hash(self, fact: MemoryFact) -> str:
        return hashlib.sha256(self.build(fact).encode("utf-8")).hexdigest()

    def content_hash_fields(
        self,
        *,
        kind: str,
        category: str,
        memory_key: str,
        content: str,
    ) -> str:
        text = self.build_fields(
            kind=kind,
            category=category,
            memory_key=memory_key,
            content=content,
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingQueryBuilder:
    """Project only the bounded query already approved by MemoryQueryBuilder."""

    def __init__(self, *, max_characters: int) -> None:
        if max_characters <= 0:
            raise ValueError("embedding query limit must be positive")
        self.max_characters = max_characters

    def build(self, query: MemoryQuery) -> str:
        return _bounded(query.text, self.max_characters)
