"""Exercise the adapter parser with the transport's decoded-response contract."""

import httpx

from yuki.domain.messages import ChatRequest, ChatResponse
from yuki.llm.json_http import JSONHTTPProvider


def parse_response(
    provider: JSONHTTPProvider, response: httpx.Response, request: ChatRequest
) -> ChatResponse:
    return provider._parse(response, request, response.json())
