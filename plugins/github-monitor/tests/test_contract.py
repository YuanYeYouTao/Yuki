from pathlib import Path

import pytest

from yuki_plugin_sdk.testing.contract import run_plugin_contract_tests

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_plugin_loads_through_real_host_contract() -> None:
    report = await run_plugin_contract_tests(PLUGIN_ROOT, yuki_version="3.8.1")

    assert report.passed, report.model_dump(mode="json")
    assert report.checks == (
        "manifest",
        "permissions",
        "entrypoint",
        "register",
        "start",
        "stop",
    )
