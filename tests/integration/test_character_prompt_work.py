"""Actual Main request prefixes across new and recovered original computer Work."""

import pytest
from tests.integration.test_computer_work_recovery import computer_work_recovery_case


@pytest.mark.parametrize(
    "change_at,crash", [("before", False), ("waiting", False), ("waiting", True)]
)
async def test_prompt_change_keeps_original_work_budget_and_uses_new_prefix(
    database,
    tmp_path,
    monkeypatch,
    change_at,
    crash,
):
    await computer_work_recovery_case(
        database, tmp_path, monkeypatch, "wait-control", crash, prompt_change_at=change_at
    )
