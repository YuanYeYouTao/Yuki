"""Search-only cache upgrades leave explicit page and original source data intact."""

import hashlib
import json
from dataclasses import asdict

from tests.unit.test_native_search_bridge import _bridge

from yuki.domain.messages import (
    ChatResponse,
    CitationOrigin,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ResponseCitation,
)
from yuki.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource


async def test_new_search_does_not_reuse_old_extracted_result_but_keeps_page_cache(tmp_path):
    bridge, model = _bridge(tmp_path)
    request = WebSearchRequest("same query")
    source = WebSearchSource(
        "original", "Original", "https://example.com/page", "example.com", "", "old fetched body"
    )
    legacy = WebSearchResponse(request.query, (source,), "old-response", 1, provider=bridge.name)
    namespace = hashlib.sha256(
        json.dumps(
            {
                "version": "independent-native-search-v1",
                "profile": bridge.profile.model_dump(mode="json"),
                "credential": "synthetic",
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).digest()
    original_request = {**asdict(request), "extract_max_results": 3}
    key = hashlib.sha256(
        namespace + json.dumps(original_request, sort_keys=True, default=str).encode()
    ).hexdigest()
    bridge.state.access(key, legacy)
    page_key = hashlib.sha256(f"direct-page-v2:{source.url}:read question".encode()).hexdigest()
    page = WebSearchResponse("read question", (source,), None, 0, provider="direct_http")
    bridge.state.access(page_key, page)
    from yuki.web.page_extractor import DirectPageExtractor

    reader = DirectPageExtractor(state=bridge.state, timeout_seconds=1)
    try:
        result = await bridge.search(request)
        assert result.provider_request_id == "actual-response"
        assert result.sources[0].relevant_content == ""
        assert await bridge.search(request) == result
        assert len(model.requests) == 1
        assert await reader.extract(source.url, "read question") == source
        assert bridge.state.access(key) == legacy
        assert bridge.state.access(page_key) == page
    finally:
        await reader.close()
        await bridge.close()


async def test_completed_upstream_open_page_is_evidence_without_local_page_request(tmp_path):
    response = ChatResponse(
        "Original upstream synthesis",
        0,
        native_tool_events=(
            NativeToolEvent(
                NativeToolType.WEB_SEARCH,
                "opened",
                NativeToolStatus.COMPLETED,
                action_type="open_page",
                url="https://example.com/opened",
            ),
            NativeToolEvent(
                NativeToolType.WEB_SEARCH,
                "failed",
                NativeToolStatus.FAILED,
                action_type="open_page",
                url="https://example.com/failed",
            ),
        ),
        citations=(
            ResponseCitation(
                "https://example.com/cited",
                snippet="upstream body",
                origin=CitationOrigin.ANNOTATION,
            ),
        ),
    )
    bridge, model = _bridge(tmp_path, response)
    try:
        result = await bridge.search(WebSearchRequest("query"))
        assert [item.url for item in result.sources] == [
            "https://example.com/cited",
            "https://example.com/opened",
        ]
        assert result.sources[0].relevant_content == "upstream body"
        assert result.provider_summary == response.content
        assert result.partial_failure
        assert len(model.requests) == 1
        bridge.extractor.extract.assert_not_awaited()
    finally:
        await bridge.close()
