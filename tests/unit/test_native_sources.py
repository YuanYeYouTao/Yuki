"""Provider-native web provenance must come from provider metadata."""

from qq_ai_bot.domain.messages import (
    CitationOrigin,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ResponseCitation,
)
from qq_ai_bot.web.native_sources import recover_native_web_response


def test_answer_urls_do_not_become_native_search_sources() -> None:
    response = recover_native_web_response(
        events=(
            NativeToolEvent(
                tool_type=NativeToolType.WEB_SEARCH,
                call_id="search-1",
                status=NativeToolStatus.COMPLETED,
                action_type="search",
                query="test query",
            ),
        ),
        citations=(
            ResponseCitation(
                url="https://example.com/claimed",
                origin=CitationOrigin.ANSWER_TEXT,
            ),
        ),
        answer_text="I searched https://example.com/claimed",
    )

    assert response.query == "test query"
    assert response.sources == ()
    assert response.partial_failure is True


def test_provider_metadata_and_completed_open_page_are_sources() -> None:
    response = recover_native_web_response(
        events=(
            NativeToolEvent(
                tool_type=NativeToolType.WEB_SEARCH,
                call_id="open-1",
                status=NativeToolStatus.COMPLETED,
                action_type="open_page",
                url="https://example.org/page",
            ),
        ),
        citations=(
            ResponseCitation(
                url="https://example.com/grounded",
                title="Grounded",
                origin=CitationOrigin.ANNOTATION,
            ),
        ),
        answer_text="Unrelated https://example.net/unverified",
    )

    assert [source.url for source in response.sources] == [
        "https://example.com/grounded",
        "https://example.org/page",
    ]
    assert response.partial_failure is False


def test_failed_server_action_keeps_partial_failure_with_other_evidence() -> None:
    response = recover_native_web_response(
        events=(
            NativeToolEvent(
                tool_type=NativeToolType.WEB_SEARCH,
                call_id="search-1",
                status=NativeToolStatus.FAILED,
                action_type="search",
            ),
        ),
        citations=(ResponseCitation(url="https://example.com/grounded"),),
        answer_text="",
    )

    assert len(response.sources) == 1
    assert response.partial_failure is True
