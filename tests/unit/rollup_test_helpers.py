"""Valid output for rollup fakes whose tests concern scheduling, not prose."""

import json

from qq_ai_bot.conversation.rollup.models import RollupCandidate
from qq_ai_bot.domain.messages import ChatRequest


def model_summary(request: ChatRequest, continuity: str = "semantic catch-up summary") -> str:
    body = request.messages[-1].content
    ids = json.loads(body.split("Available internal source_event_ids: ", 1)[1].split("\n", 1)[0])
    return _output(ids[:1], continuity)


def candidate_summary(candidate: RollupCandidate, continuity: str) -> str:
    return _output([candidate.events[0].id], continuity)


def _output(ids: list[int], continuity: str) -> str:
    return json.dumps(
        {
            "schema": "conversation_rollup_v1",
            "continuity": continuity,
            "source_event_ids": ids,
            "open_issues": [],
            "corrections": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
