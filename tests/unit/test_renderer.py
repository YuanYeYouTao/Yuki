"""Outbound-only model text cleanup regressions."""

from __future__ import annotations

import pytest

from qq_ai_bot.services.renderer import clean_model_output


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("#37483 正文", "正文"),
        ("#37483>正文", "正文"),
        ("#37483|回复:#37480/Yuki>正文", "正文"),
        ("#37483 第一行\n#37484>第二行", "第一行\n第二行"),
    ),
)
def test_clean_model_output_strips_line_leading_event_numbers(
    raw: str,
    expected: str,
) -> None:
    assert clean_model_output(raw, max_characters=12_000) == expected
