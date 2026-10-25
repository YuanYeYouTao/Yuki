"""Budget counts use the actual JSON encoding without allocating its full text."""

import json
import random

import pytest

from yuki.capabilities.results import ToolExecutionResult, artifact_page_fits, artifact_page_size
from yuki.tool_results.json_length import json_character_count, json_string_prefix


@pytest.mark.parametrize("compact", [False, True])
def test_saturated_counts_match_real_json_for_unicode_and_nested_values(compact):
    rng = random.Random(7)
    leaves = [None, True, False, -0.0, 1.2e-7, float("inf"), '中😀\n\x00\\"', 100000]

    def value(depth):
        if not depth or rng.randrange(3) == 0:
            return rng.choice(leaves)
        if rng.randrange(2):
            return [value(depth - 1) for _ in range(rng.randrange(5))]
        return {str(index) + "中": value(depth - 1) for index in range(rng.randrange(5))}

    for item in [value(4) for _ in range(100)] + [{None: 0, False: 1, 2.5: "x"}]:
        encoded = json.dumps(
            item, ensure_ascii=False, default=str, separators=(",", ":") if compact else None
        )
        assert json_character_count(item, compact=compact) == len(encoded)
        for budget in (0, 1, len(encoded) // 2, len(encoded) - 1, len(encoded), len(encoded) + 1):
            assert json_character_count(item, compact=compact, limit=budget) == min(
                len(encoded), budget + 1
            )


def test_saturated_memo_can_be_expanded_and_shared_subtrees_count_each_occurrence():
    subtree = ["x" * 100, {"quoted": "\n" * 100}]
    cache = {}
    assert json_character_count(subtree, limit=10, cache=cache) == 11
    expected = len(json.dumps(subtree, ensure_ascii=False))
    assert json_character_count(subtree, limit=expected, cache=cache) == expected
    assert json_character_count([subtree, subtree], cache=cache) == 2 * expected + 4


def test_string_prefix_counts_original_characters_and_json_escapes():
    text = '中😀\n\x01\\"tail'
    for budget in range(30):
        fitting = [
            length
            for length in range(len(text) + 1)
            if len(json.dumps(text[:length], ensure_ascii=False)) - 2 <= budget
        ]
        assert json_string_prefix(text, budget) == max(fitting)


def test_error_budget_uses_the_actual_error_envelope():
    page = {"error_code": "artifact_value_too_large", "detail": "目标太大", "path": ["中"]}
    actual = ToolExecutionResult(
        ok=False,
        data={"path": ["中"]},
        error_code=page["error_code"],
        public_message=page["detail"],
        provider_id="artifacts",
        tool_name="read_tool_artifact",
    ).model_payload()
    size = len(json.dumps(actual, ensure_ascii=False, default=str))
    assert artifact_page_size(page) == size
    assert artifact_page_fits(page, size)
    assert not artifact_page_fits(page, size - 1)
