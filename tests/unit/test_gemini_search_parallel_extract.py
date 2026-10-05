"""Bounded real bridge extraction order, failures, HTTP limits, and joined cancellation."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.unit.test_gemini_search_bridge import grounded_response, profile

from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebSearchRequest, WebSearchSource
from qq_ai_bot.web.tavily import TavilyWebSearchProvider

URLS = tuple(f"https://source-{number}.example/page" for number in range(5))


@asynccontextmanager
async def bridge_fixture(tmp_path, fallback):
    wires = []

    def respond(request):
        wires.append(json.loads(request.content))
        response = grounded_response()
        response["candidates"][0]["groundingMetadata"]["groundingChunks"] = [
            {"web": {"uri": url, "title": f"title-{number}"}} for number, url in enumerate(URLS)
        ]
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        base_url="https://example.com/v1beta/", transport=httpx.MockTransport(respond)
    ) as client:
        provider = GeminiProvider(
            base_url="https://example.com/v1beta/",
            api_key="synthetic",
            timeout_seconds=20,
            max_retries=0,
            client=client,
        )
        bridge = GeminiSearchBridge(
            profile=profile(),
            credential="synthetic",
            provider=provider,
            state=BridgeState(tmp_path / "cache.db"),
            fallback=fallback,
        )
        try:
            yield bridge, wires
        finally:
            await bridge.close()


class Fallback:
    def __init__(self):
        self.calls = []
        self.search = AsyncMock(side_effect=AssertionError("unexpected fallback search"))
        self.close = AsyncMock()

    async def extract(self, url, query):
        self.calls.append(url)
        return WebSearchSource("page", "extracted-title", url, "example", "s" * 1100, "b" * 2600)


@pytest.mark.parametrize("extract_max,expected", [(0, 0), (1, 1), (3, 3), (100, 3)])
async def test_extract_upper_bound_original_sources_and_single_model_budget(
    tmp_path, extract_max, expected
):
    fallback = Fallback()
    control = SimpleNamespace(validate=AsyncMock(), reserve_request=AsyncMock())
    token = current_work_control.set(control)
    try:
        async with bridge_fixture(tmp_path, fallback) as (bridge, wires):
            result = await bridge.search(
                WebSearchRequest("query", max_results=5, extract_max_results=extract_max)
            )
            repeat = await bridge.search(
                WebSearchRequest("query", max_results=5, extract_max_results=extract_max)
            )
    finally:
        current_work_control.reset(token)
    assert repeat == result
    assert len(wires) == 1 and fallback.calls == list(URLS[:expected])
    assert tuple(source.url for source in result.sources) == URLS
    assert tuple(source.title for source in result.sources) == tuple(f"title-{n}" for n in range(5))
    assert all(
        len(source.snippet) == 1000 and len(source.relevant_content) == 2500
        for source in result.sources[:expected]
    )
    assert all(source.relevant_content == "" for source in result.sources[expected:])
    assert not result.partial_failure
    control.reserve_request.assert_awaited_once_with(auxiliary=True)
    fallback.search.assert_not_awaited()


async def test_pages_are_parallel_but_response_order_stays_citation_order(tmp_path):
    fallback = Fallback()
    all_entered = asyncio.Event()
    releases = [asyncio.Event() for _ in range(3)]
    completed = []

    async def extract(url, query):
        index = URLS.index(url)
        fallback.calls.append(url)
        if len(fallback.calls) == 3:
            all_entered.set()
        await releases[index].wait()
        completed.append(index)
        return WebSearchSource("page", "page", url, "example", str(index), str(index))

    fallback.extract = extract
    async with bridge_fixture(tmp_path, fallback) as (bridge, _wires):
        pending = asyncio.create_task(
            bridge.search(WebSearchRequest("query", max_results=3, extract_max_results=3))
        )
        await asyncio.wait_for(all_entered.wait(), 2)
        for index in (2, 0, 1):
            releases[index].set()
            await asyncio.sleep(0)
        result = await pending
    assert completed == [2, 0, 1]
    assert [source.relevant_content for source in result.sources] == ["0", "1", "2"]


async def test_failed_page_keeps_other_results_and_partial_is_not_cached(tmp_path):
    fallback = Fallback()
    failing = True

    async def extract(url, query):
        fallback.calls.append(url)
        if failing and url == URLS[1]:
            raise WebSearchError("extract_failed", "synthetic")
        return WebSearchSource("page", "page", url, "example", "ok", "ok")

    fallback.extract = extract
    async with bridge_fixture(tmp_path, fallback) as (bridge, wires):
        request = WebSearchRequest("query", max_results=3, extract_max_results=3)
        first = await bridge.search(request)
        assert first.partial_failure
        assert [source.relevant_content for source in first.sources] == ["ok", "", "ok"]
        failing = False
        recovered = await bridge.search(request)
        repeat = await bridge.search(request)
    assert recovered == repeat and not recovered.partial_failure
    assert len(wires) == 2 and len(fallback.calls) == 6
    fallback.search.assert_not_awaited()


async def test_real_tavily_global_http_semaphore_still_bounds_parallel_pages(tmp_path):
    active = maximum = entered = 0
    two_on_wire = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        nonlocal active, maximum, entered
        assert request.url.path == "/extract"
        active += 1
        entered += 1
        maximum = max(maximum, active)
        if entered == 2:
            two_on_wire.set()
        try:
            await release.wait()
            payload = json.loads(request.content)
            assert len(payload["urls"]) == 1
            return httpx.Response(
                200,
                json={"results": [{"url": payload["urls"][0], "raw_content": "synthetic page"}]},
            )
        finally:
            active -= 1

    async with httpx.AsyncClient(
        base_url="https://api.tavily.com", transport=httpx.MockTransport(respond)
    ) as client:
        fallback = TavilyWebSearchProvider(
            api_key="synthetic", global_concurrency=2, max_retries=0, client=client
        )
        async with bridge_fixture(tmp_path, fallback) as (bridge, wires):
            pending = asyncio.create_task(
                bridge.search(WebSearchRequest("query", max_results=3, extract_max_results=3))
            )
            await asyncio.wait_for(two_on_wire.wait(), 2)
            assert entered == 2 and active == 2
            release.set()
            result = await pending
    assert maximum == 2 and entered == 3 and active == 0
    assert len(wires) == 1 and not result.partial_failure


@pytest.mark.parametrize("fatal", [False, True])
async def test_cancel_or_unexpected_failure_joins_all_extractions_before_slot_release(
    tmp_path, fatal
):
    fallback = Fallback()
    all_entered = asyncio.Event()
    fail = asyncio.Event()
    cleaning = asyncio.Event()
    allow_cleanup = asyncio.Event()
    active = 0
    joined = []
    cancellations = []

    async def extract(url, query):
        nonlocal active
        index = URLS.index(url)
        fallback.calls.append(url)
        active += 1
        if active == 3:
            all_entered.set()
        try:
            if fatal and index == 0:
                await fail.wait()
                raise RuntimeError("synthetic unexpected extract failure")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append(index)
            raise
        finally:
            cleaning.set()
            await allow_cleanup.wait()
            active -= 1
            joined.append(index)

    fallback.extract = extract
    async with bridge_fixture(tmp_path, fallback) as (bridge, wires):
        pending = asyncio.create_task(
            bridge.search(WebSearchRequest("query", max_results=3, extract_max_results=3))
        )
        await asyncio.wait_for(all_entered.wait(), 2)
        if fatal:
            fail.set()
            # Allow the first fatal child to finish; siblings must then be cancelled and joined.
            allow_cleanup.set()
            with pytest.raises(RuntimeError, match="unexpected extract failure"):
                await pending
        else:
            pending.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            assert active == 3 and bridge.slot.locked() and not pending.done()
            pending.cancel()
            await asyncio.sleep(0)
            assert active == 3 and bridge.slot.locked() and not pending.done()
            allow_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert active == 0 and sorted(joined) == [0, 1, 2] and not bridge.slot.locked()
        assert len(wires) == 1
        fallback.search.assert_not_awaited()
        # Failed/cancelled output was never published to the cache.
        fallback.extract = Fallback().extract
        recovered = await bridge.search(
            WebSearchRequest("query", max_results=3, extract_max_results=3)
        )
        assert not recovered.partial_failure and len(wires) == 2
    assert sorted(cancellations) == ([1, 2] if fatal else [0, 1, 2])
