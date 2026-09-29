"""Recover provider-native web provenance without inventing source metadata."""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from qq_ai_bot.domain.messages import (
    CitationOrigin,
    NativeToolEvent,
    NativeToolStatus,
    ResponseCitation,
)
from qq_ai_bot.web.base import WebSearchError, normalize_public_url
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


def recover_native_web_response(
    *,
    events: tuple[NativeToolEvent, ...],
    citations: tuple[ResponseCitation, ...],
    answer_text: str,
) -> WebSearchResponse:
    """Keep only URLs returned by provider citation or server-tool metadata."""

    # A model can write a plausible URL without searching. Keep the answer
    # visible, but never promote its text to a provider search source.
    del answer_text
    candidates = [
        citation
        for citation in citations
        if citation.origin in {CitationOrigin.ANNOTATION, CitationOrigin.OPEN_PAGE_ACTION}
    ]
    candidates.extend(
        ResponseCitation(
            url=event.url,
            origin=CitationOrigin.OPEN_PAGE_ACTION,
            call_id=event.call_id,
        )
        for event in events
        if event.status is NativeToolStatus.COMPLETED
        and event.action_type == "open_page"
        and event.url
    )
    seen: set[str] = set()
    sources: list[WebSearchSource] = []
    for citation in candidates:
        try:
            url = normalize_public_url(citation.url)
        except WebSearchError:
            continue
        if url in seen:
            continue
        seen.add(url)
        host = urlsplit(url).hostname or ""
        source_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        sources.append(
            WebSearchSource(
                source_id=f"native-{source_hash}",
                title=citation.title,
                url=url,
                domain=host,
                snippet="",
                relevant_content="",
            )
        )
    query = next(
        (event.query for event in events if event.action_type == "search" and event.query),
        "",
    )
    return WebSearchResponse(
        query=query,
        sources=tuple(sources),
        provider_request_id=None,
        latency_seconds=0,
        partial_failure=not sources
        or any(event.status is NativeToolStatus.FAILED for event in events),
    )
