from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from benchmarks import terminalbench
from harbor.models.agent.context import AgentContext


@pytest.mark.parametrize("unknown_requests", [0, 1])
async def test_harbor_runs_yuki_and_preserves_usage_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_requests: int
) -> None:
    settings = SimpleNamespace(llm_model="deepseek-flash")
    config_path = str(tmp_path / "private.json")
    monkeypatch.setenv("YUKI_BENCH_CONFIG", config_path)
    loaded: list[tuple[str, Path]] = []

    def load_settings(path: str, output_dir: Path) -> SimpleNamespace:
        loaded.append((path, output_dir))
        return settings

    monkeypatch.setattr(terminalbench, "load_settings", load_settings)
    raw_environment = SimpleNamespace(environment_name="example-task")
    adapted_environment = object()
    monkeypatch.setattr(
        terminalbench,
        "HarborEnvironment",
        lambda environment: adapted_environment if environment is raw_environment else None,
    )
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_input_tokens": None,
        "physical_request_count": 2,
        "unknown_usage_request_count": unknown_requests,
    }
    run_task = AsyncMock(
        return_value={
            "task_id": "example-task",
            "status": "completed",
            "work_id": "work-123",
            "git_commit": "test-commit",
            "usage": usage,
        }
    )
    monkeypatch.setattr(terminalbench, "run_task", run_task)
    agent = terminalbench.YukiAgent(logs_dir=tmp_path, model_name="deepseek-flash")
    context = AgentContext(metadata={"prior": "retained"})

    await agent.run("Fix the repository.", raw_environment, context)

    assert loaded == [(config_path, tmp_path / "yuki")]
    run_task.assert_awaited_once_with(
        "Fix the repository.",
        adapted_environment,
        settings,
        tmp_path / "yuki",
        task_id="example-task",
        timeout_seconds=None,
    )
    assert context.n_input_tokens == (100 if unknown_requests == 0 else None)
    assert context.n_output_tokens == (20 if unknown_requests == 0 else None)
    assert context.n_cache_tokens is None
    assert context.cost_usd is None
    assert context.metadata["prior"] == "retained"
    assert context.metadata["yuki"]["usage"] == usage
    assert context.metadata["yuki"]["git_commit"] == "test-commit"


async def test_harbor_model_label_cannot_change_yuki_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YUKI_BENCH_CONFIG", str(tmp_path / "private.json"))
    monkeypatch.setattr(
        terminalbench,
        "load_settings",
        lambda *_: SimpleNamespace(llm_model="deepseek-flash"),
    )
    run_task = AsyncMock()
    monkeypatch.setattr(terminalbench, "run_task", run_task)
    agent = terminalbench.YukiAgent(logs_dir=tmp_path, model_name="another-model")

    with pytest.raises(ValueError, match="must match"):
        await agent.run("task", SimpleNamespace(environment_name="task"), AgentContext())

    run_task.assert_not_awaited()
