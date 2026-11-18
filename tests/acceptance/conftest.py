"""Phase-2 acceptance with real native agents: opt-in, never part of CI.

Every test here drives real Claude Code / Codex / ACP agents (paid model calls) and
private tmux sessions on this machine. They run only when ``YUKI_REAL_AGENT_E2E=1``;
otherwise each ``real_agent`` test is skipped at collection.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("YUKI_REAL_AGENT_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="real native agents: set YUKI_REAL_AGENT_E2E=1 to run")
    for item in items:
        if "real_agent" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    """summary.json: one line of facts per scenario artifact present after the run."""
    if os.environ.get("YUKI_REAL_AGENT_E2E") != "1":
        return
    import json
    from datetime import UTC, datetime

    from tests.acceptance.harness import RESULTS

    if not RESULTS.exists():
        return
    scenarios = {}
    for path in sorted(RESULTS.glob("A*.json")):
        value = json.loads(path.read_text())
        scenarios[path.stem] = {
            "status": value.get("status"),
            "error": value.get("error"),
            "artifact": path.name,
        }
    (RESULTS / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "pytest_exit_status": int(exitstatus),
                "scenarios": scenarios,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
