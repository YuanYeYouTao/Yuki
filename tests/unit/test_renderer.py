"""Outbound-only model text cleanup regressions."""

from __future__ import annotations

import pytest

from qq_ai_bot.services.renderer import sanitize_model_output


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("#37483 正文", "正文"),
        ("#37483>正文", "正文"),
        ("#37483|回复:#37480/Yuki>正文", "正文"),
        ("#37483 第一行\n#37484>第二行", "第一行\n第二行"),
    ),
)
def test_sanitize_model_output_strips_line_leading_event_numbers(
    raw: str,
    expected: str,
) -> None:
    assert sanitize_model_output(raw, max_characters=12_000) == expected


def test_sanitize_model_output_preserves_model_authored_sources_and_links() -> None:
    text = (
        "结论。[1]\n\n来源：\n1. 模型选择的来源\n"
        "https://example.com/reference\n[说明](https://example.org/details)"
    )

    assert sanitize_model_output(text, max_characters=12_000) == text


@pytest.mark.parametrize(
    "tail",
    (
        '<yuki-state>{"engage":"quiet","mood":"calm"}</yuki-state>',
        '<yuki-state>{"engage":"invalid"}</yuki-state>',
        "<yuki-state>incomplete",
        '<yuki-state>{"actor":"another-person"}</yuki-state>',
    ),
)
def test_self_report_control_tail_never_reaches_outbound_text(tail: str) -> None:
    assert sanitize_model_output(f"可见正文\n{tail}", max_characters=12_000) == "可见正文"
