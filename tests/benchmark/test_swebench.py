from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from benchmarks import swebench


def task(instance_id: str = "owner__repo-1") -> dict:
    return {
        "instance_id": instance_id,
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "Repair the parser.",
        "image": f"swebench/task.{instance_id}:latest",
    }


def dataset_modules(monkeypatch, rows: list[dict]) -> list[dict]:
    calls = []
    datasets = ModuleType("datasets")

    def load_dataset(name, **kwargs):
        calls.append({"name": name, **kwargs})
        return rows

    datasets.load_dataset = load_dataset
    huggingface = ModuleType("huggingface_hub")
    huggingface.HfApi = lambda: SimpleNamespace(
        dataset_info=lambda *args, **kwargs: SimpleNamespace(sha="resolved-hf-revision")
    )
    monkeypatch.setitem(sys.modules, "datasets", datasets)
    monkeypatch.setitem(sys.modules, "huggingface_hub", huggingface)
    return calls


def test_load_pins_revision_and_omits_answer_fields(monkeypatch):
    source = {
        **task(),
        "patch": "gold solution",
        "test_patch": "hidden tests",
        "hints_text": "answer hint",
        "FAIL_TO_PASS": ["hidden_test"],
        "PASS_TO_PASS": [],
    }
    calls = dataset_modules(monkeypatch, [source])

    revision, records = swebench.load_tasks(swebench.DEFAULT_DATASET, "dev", "main")

    assert revision == "resolved-hf-revision"
    assert records == [task()]
    assert calls == [
        {
            "name": swebench.DEFAULT_DATASET,
            "split": "dev",
            "revision": revision,
        }
    ]
    assert "gold solution" not in swebench.instruction_for(records[0])


def test_old_dataset_without_image_does_not_guess_container(monkeypatch):
    source = task()
    del source["image"]
    dataset_modules(monkeypatch, [source])

    with pytest.raises(ValueError, match="no official image field"):
        swebench.load_tasks(swebench.DEFAULT_DATASET, "test", None)


async def test_prepare_selects_all_or_named_tasks_without_creating_containers(
    monkeypatch, tmp_path
):
    tasks = [task(), task("owner__repo-2")]
    monkeypatch.setattr(swebench, "load_tasks", lambda *args: ("hf-sha", tasks))

    selected = await swebench.prepare_run(tmp_path / "selected", instance_ids=["owner__repo-2"])
    all_tasks = await swebench.prepare_run(tmp_path / "all")

    assert selected["tasks"] == [tasks[1]]
    assert all_tasks["tasks"] == tasks
    assert selected["revision"] == "hf-sha"
    assert not (tmp_path / "all" / "tasks").exists()
    with pytest.raises(ValueError, match="Unknown task IDs"):
        await swebench.prepare_run(tmp_path / "selected", instance_ids=[tasks[0]["instance_id"]])


async def test_resume_keeps_original_container_and_filesystem(monkeypatch, tmp_path):
    swebench._write_json(
        tmp_path / "environment.json",
        {
            "container_id": "original-container",
            "initialized": True,
        },
    )
    calls = []

    async def docker(*args):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return json.dumps([{"State": {"Running": False}}])
        return "original-container"

    monkeypatch.setattr(swebench, "_docker", docker)
    result = await swebench._container(task(), tmp_path, "bridge")

    assert result == "original-container"
    assert calls == [
        ("container", "inspect", "original-container"),
        ("container", "start", "original-container"),
    ]


async def test_missing_original_container_does_not_replay(monkeypatch, tmp_path):
    swebench._write_json(
        tmp_path / "environment.json",
        {
            "container_id": "lost-container",
            "initialized": True,
        },
    )
    calls = []

    async def docker(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, ["docker", *args])

    monkeypatch.setattr(swebench, "_docker", docker)
    with pytest.raises(RuntimeError, match="Original task container"):
        await swebench._container(task(), tmp_path, "bridge")
    assert calls == [("container", "inspect", "lost-container")]


async def test_patch_contains_untracked_and_agent_committed_changes(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Benchmark Test")
    git("config", "user.email", "benchmark@example.invalid")
    (tmp_path / "existing.py").write_text("answer = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    base_commit = git("rev-parse", "HEAD")
    (tmp_path / "existing.py").write_text("answer = 2\n", encoding="utf-8")
    git("commit", "-am", "agent commit")
    (tmp_path / "new.py").write_text("added = True\n", encoding="utf-8")

    class LocalEnvironment:
        async def run(self, command, **kwargs):
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=tmp_path,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
            )
            return {
                "return_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

    patch = await swebench.collect_patch(LocalEnvironment(), base_commit)

    assert "+answer = 2" in patch
    assert "+added = True" in patch
    assert "new file mode" in patch


def runner_modules(monkeypatch, results: dict, *, model: str = "test-model") -> list:
    calls = []
    config = ModuleType("benchmarks.config")
    config.load_settings = lambda *args: SimpleNamespace(llm_model=model)
    config.public_settings = lambda settings: {"model": settings.llm_model, "provider": "fake"}
    environment = ModuleType("benchmarks.environment")
    environment.ContainerEnvironment = lambda container_id: SimpleNamespace(
        container_id=container_id
    )
    runner = ModuleType("benchmarks.runner")

    async def run_task(instruction, environment, settings, output_dir, **kwargs):
        calls.append(
            {"instruction": instruction, "container_id": environment.container_id, **kwargs}
        )
        return results[kwargs["task_id"]]

    async def container(*args):
        return "original-container"

    async def collect_patch(*args):
        return "diff --git a/file b/file\n"

    runner.run_task = run_task
    monkeypatch.setitem(sys.modules, "benchmarks.config", config)
    monkeypatch.setitem(sys.modules, "benchmarks.environment", environment)
    monkeypatch.setitem(sys.modules, "benchmarks.runner", runner)
    monkeypatch.setattr(swebench, "_container", container)
    monkeypatch.setattr(swebench, "collect_patch", collect_patch)
    return calls


def run_manifest(tmp_path: Path, tasks: list[dict], **extra):
    swebench._write_json(
        tmp_path / "manifest.json",
        {
            "dataset": swebench.DEFAULT_DATASET,
            "split": "dev",
            "revision": "hf-sha",
            "network": "bridge",
            "tasks": tasks,
            **extra,
        },
    )


async def test_timeout_attempt_is_scored_and_final_predictions_are_not_replayed(
    monkeypatch, tmp_path
):
    tasks = [task(), task("owner__repo-2")]
    run_manifest(tmp_path, tasks)
    calls = runner_modules(
        monkeypatch,
        {
            tasks[0]["instance_id"]: {"outcome": "timeout"},
            tasks[1]["instance_id"]: {"outcome": "completed"},
        },
    )

    first = await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))
    second = await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))

    assert first["completed"] == [entry["instance_id"] for entry in tasks]
    assert second["skipped"] == first["completed"]
    assert len(calls) == 2
    predictions = swebench._predictions(tmp_path / "predictions.jsonl")
    assert set(predictions) == set(first["completed"])
    assert predictions[tasks[0]["instance_id"]]["model_patch"]


async def test_changed_model_cannot_replace_original_run(monkeypatch, tmp_path):
    run_manifest(tmp_path, [task()], settings={"model": "original", "provider": "fake"})
    calls = runner_modules(monkeypatch, {}, model="different")

    with pytest.raises(ValueError, match="configuration changed"):
        await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))
    assert calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "uncertain"},
        {"status": "cancelled", "error": "task_cancelled"},
    ],
)
async def test_uncertain_attempt_keeps_original_task_pending(monkeypatch, tmp_path, outcome):
    run_manifest(tmp_path, [task()])
    calls = runner_modules(monkeypatch, {task()["instance_id"]: outcome})

    result = await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))
    repeated = await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))

    assert result["pending"] == [task()["instance_id"]]
    assert repeated["pending"] == result["pending"]
    assert len(calls) == 1
    assert calls[0]["resume"] is False
    assert not (tmp_path / "predictions.jsonl").exists()
    await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"), resume=True)
    assert len(calls) == 2
    assert calls[1]["resume"] is True


async def test_native_cancel_decision_still_submits_its_patch(monkeypatch, tmp_path):
    run_manifest(tmp_path, [task()])
    runner_modules(monkeypatch, {task()["instance_id"]: {"status": "cancelled", "error": None}})

    result = await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))

    assert result["completed"] == [task()["instance_id"]]
    assert task()["instance_id"] in swebench._predictions(tmp_path / "predictions.jsonl")


async def test_terminal_result_after_interruption_is_finalized_without_another_model_call(
    monkeypatch,
    tmp_path,
):
    run_manifest(tmp_path, [task()])
    task_dir = tmp_path / "tasks" / task()["instance_id"]
    swebench._write_json(task_dir / "result.json", {"status": "timeout"})
    (task_dir / "patch.diff").write_text("previously collected patch\n", encoding="utf-8")
    calls = runner_modules(monkeypatch, {})

    result = await swebench.run_benchmark(tmp_path, config_path=Path("config.toml"))

    assert result["completed"] == [task()["instance_id"]]
    assert calls == []
    prediction = swebench._predictions(tmp_path / "predictions.jsonl")[task()["instance_id"]]
    assert prediction["model_patch"] == "previously collected patch\n"


async def test_cleanup_removes_only_finalized_owned_containers(monkeypatch, tmp_path):
    task_id = task()["instance_id"]
    state_file = tmp_path / "tasks" / task_id / "environment.json"
    swebench._write_json(state_file, {"container_id": "original-container"})
    (tmp_path / "predictions.jsonl").write_text(
        json.dumps({"instance_id": task_id, "model_patch": "patch"}) + "\n", encoding="utf-8"
    )
    swebench._write_json(
        tmp_path / "tasks" / "unfinished" / "environment.json",
        {
            "container_id": "unfinished-container",
        },
    )
    calls = []

    async def docker(*args):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {
                                "io.yuki.benchmark": "swebench",
                                "io.yuki.benchmark.task": task_id,
                            }
                        }
                    }
                ]
            )
        return "original-container"

    monkeypatch.setattr(swebench, "_docker", docker)
    assert await swebench.cleanup_run(tmp_path) == [task_id]
    assert calls == [
        ("container", "inspect", "original-container"),
        ("container", "rm", "--force", "original-container"),
    ]
    assert swebench._read_json(state_file)["removed"]


async def test_cleanup_does_not_delete_a_container_with_different_ownership(monkeypatch, tmp_path):
    task_id = task()["instance_id"]
    swebench._write_json(
        tmp_path / "tasks" / task_id / "environment.json",
        {
            "container_id": "someone-elses-container",
        },
    )
    (tmp_path / "predictions.jsonl").write_text(
        json.dumps({"instance_id": task_id, "model_patch": "patch"}) + "\n", encoding="utf-8"
    )

    async def docker(*args):
        assert args[:2] == ("container", "inspect")
        return json.dumps([{"Config": {"Labels": {}}}])

    monkeypatch.setattr(swebench, "_docker", docker)
    with pytest.raises(ValueError, match="not owned by task"):
        await swebench.cleanup_run(tmp_path)


def test_official_evaluation_uses_pinned_revision_and_image(monkeypatch, tmp_path):
    run_manifest(tmp_path, [task()])
    swebench._write_json(
        tmp_path / "tasks" / task()["instance_id"] / "image.json",
        {
            "image_id": "sha256:pinned-image",
        },
    )
    calls = dataset_modules(monkeypatch, [{**task(), "patch": "gold", "test_patch": "test"}])
    invoked = []

    def run(command, **kwargs):
        invoked.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(swebench.subprocess, "run", run)
    result = swebench.evaluate(tmp_path, run_id="evaluation-001", workers=1, timeout_seconds=1800)

    assert result == 0
    assert calls[0]["revision"] == "hf-sha"
    dataset = swebench._read_json(tmp_path / "evaluation" / "dataset.json")
    assert dataset[0]["image"] == "sha256:pinned-image"
    command = invoked[0][0]
    assert command[:3] == [sys.executable, "-m", "swebench.harness.run_evaluation"]
    assert command[command.index("--instance_ids") + 1] == task()["instance_id"]
    assert command[command.index("--run_id") + 1] == "evaluation-001"
