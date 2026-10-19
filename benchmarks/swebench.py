"""Run Yuki on SWE-bench, then hand its patches to the official evaluator.

Only the issue text and the task container reach the agent. Dataset answer and
grading fields are deliberately absent from the saved task manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DATASET = "SWE-bench/SWE-bench_Lite"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_tasks(dataset: str, split: str, revision: str | None) -> tuple[str, list[dict]]:
    """Resolve an immutable HF revision and retain only agent input metadata."""
    from datasets import load_dataset
    from huggingface_hub import HfApi

    resolved = HfApi().dataset_info(dataset, revision=revision).sha
    records = load_dataset(dataset, split=split, revision=resolved)
    tasks = []
    for record in records:
        # v5 datasets publish the actual instance image. Do not guess an image
        # name from an older dataset's instance_id or silently change revisions.
        if not record.get("image"):
            raise ValueError(
                f"{record['instance_id']} has no official image field at {resolved}; "
                "choose a current SWE-bench dataset revision"
            )
        tasks.append(
            {
                key: record[key]
                for key in ("instance_id", "repo", "base_commit", "problem_statement", "image")
            }
        )
    return resolved, tasks


def select_tasks(tasks: list[dict], instance_ids: list[str] | None) -> list[dict]:
    if not instance_ids:
        return tasks
    wanted = set(instance_ids)
    missing = wanted - {task["instance_id"] for task in tasks}
    if missing:
        raise ValueError(f"Unknown task IDs in this run: {', '.join(sorted(missing))}")
    return [task for task in tasks if task["instance_id"] in wanted]


async def _docker(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            ["docker", *arguments],
            output=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
    return stdout.decode("utf-8", errors="replace")


async def _image(task: dict, task_dir: Path, *, pull: bool = False) -> dict:
    image_file = task_dir / "image.json"
    previous = _read_json(image_file) if image_file.exists() else None
    reference = previous["image_id"] if previous else task["image"]
    if pull and previous is None:
        await _docker("image", "pull", reference)
    try:
        inspected = json.loads(await _docker("image", "inspect", reference))[0]
    except subprocess.CalledProcessError:
        if previous:
            digests = previous["repo_digests"]
            if not digests:
                raise RuntimeError(
                    f"Pinned local image {reference} is no longer available"
                ) from None
            reference = digests[0]
        await _docker("image", "pull", reference)
        inspected = json.loads(await _docker("image", "inspect", reference))[0]
    metadata = {
        "image": task["image"],
        "image_id": inspected["Id"],
        "repo_digests": inspected.get("RepoDigests") or [],
    }
    if previous and metadata["image_id"] != previous["image_id"]:
        raise ValueError(f"Image identity changed for {task['instance_id']}")
    _write_json(image_file, metadata)
    return metadata


async def prepare_run(
    output_dir: Path,
    *,
    dataset: str = DEFAULT_DATASET,
    split: str = "dev",
    revision: str | None = None,
    instance_ids: list[str] | None = None,
    pull: bool = False,
    network: str = "bridge",
) -> dict:
    """Save a selected, pinned task set; optionally pull images, without inference."""
    output_dir = Path(output_dir)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        requested = (dataset, split, network)
        existing = (manifest["dataset"], manifest["split"], manifest["network"])
        if requested != existing or (revision and revision != manifest["revision"]):
            raise ValueError(
                "This output directory already belongs to another dataset configuration"
            )
        selected = select_tasks(manifest["tasks"], instance_ids)
    else:
        resolved, tasks = await asyncio.to_thread(load_tasks, dataset, split, revision)
        selected = select_tasks(tasks, instance_ids)
        manifest = {
            "benchmark": "swebench",
            "dataset": dataset,
            "split": split,
            "revision": resolved,
            "network": network,
            "tasks": selected,
        }
        _write_json(manifest_path, manifest)
    if pull:
        for task in selected:
            await _image(task, output_dir / "tasks" / task["instance_id"], pull=True)
    return manifest


async def _container(task: dict, task_dir: Path, network: str) -> str:
    state_file = task_dir / "environment.json"
    if state_file.exists():
        state = _read_json(state_file)
        container_id = state["container_id"]
        # An original execution is resumed only in its original filesystem.
        # Missing containers are not treated as permission to replay the task.
        try:
            container = json.loads(await _docker("container", "inspect", container_id))[0]
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                f"Original task container {container_id} is unavailable; "
                "use a new output directory for a new attempt"
            ) from error
        if not container["State"]["Running"]:
            await _docker("container", "start", container_id)
    else:
        image = await _image(task, task_dir)
        container_id = (
            await _docker(
                "run",
                "--detach",
                "--name",
                f"yuki-swebench-{uuid.uuid4().hex}",
                "--label",
                "io.yuki.benchmark=swebench",
                "--label",
                f"io.yuki.benchmark.task={task['instance_id']}",
                "--network",
                network,
                "--workdir",
                "/testbed",
                image["image_id"],
                "/bin/bash",
                "-lc",
                "tail -f /dev/null",
            )
        ).strip()
        state = {"container_id": container_id, "initialized": False, **image}
        _write_json(state_file, state)
    # The official image may add an empty setup commit. Reset only this newly
    # owned container, never a resumed task, to the dataset's declared base.
    if not state["initialized"]:
        await _docker(
            "exec",
            "--workdir",
            "/testbed",
            container_id,
            "/bin/bash",
            "-lc",
            f"git reset --hard {shlex.quote(task['base_commit'])} && git clean -fd",
        )
        state["initialized"] = True
        _write_json(state_file, state)
    return container_id


def instruction_for(task: dict) -> str:
    return (
        "Resolve the following issue in the repository at /testbed. "
        "Modify the repository files and run relevant local tests. "
        "Do not look up the original solution, its pull request, benchmark answers, "
        "or grading tests online. Your final patch will be collected from the repository.\n\n"
        + task["problem_statement"]
    )


async def collect_patch(environment: Any, base_commit: str) -> str:
    # Intent-to-add includes newly created, nonignored files. Comparing against
    # base_commit also retains changes the agent has already committed.
    result = await environment.run(
        f"git add --intent-to-add -- . && git diff --binary {shlex.quote(base_commit)} --",
        cwd="/testbed",
    )
    if result["return_code"]:
        raise RuntimeError(f"Cannot collect task patch: {result['stderr']}")
    return result["stdout"]


def _predictions(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {record["instance_id"]: record for record in records}


def _unfinished(result: dict | None) -> bool:
    if result is None:
        return True
    status = result.get("status", result.get("outcome", "unknown"))
    return status in {"running", "interrupted", "uncertain", "unknown", "waiting_external"} or (
        status == "cancelled" and result.get("error") == "task_cancelled"
    )


async def run_benchmark(
    output_dir: Path,
    *,
    config_path: Path,
    instance_ids: list[str] | None = None,
    timeout_seconds: float | None = None,
    resume: bool = False,
) -> dict:
    from benchmarks.config import load_settings, public_settings
    from benchmarks.environment import ContainerEnvironment
    from benchmarks.runner import run_task

    output_dir = Path(output_dir)
    manifest_path = output_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    selected = select_tasks(manifest["tasks"], instance_ids)
    settings = load_settings(config_path, output_dir)
    identity = public_settings(settings)
    if "settings" in manifest and manifest["settings"] != identity:
        raise ValueError("Run configuration changed; use a new output directory for another system")
    if "settings" not in manifest:
        manifest["settings"] = identity
        _write_json(manifest_path, manifest)
    predictions_path = output_dir / "predictions.jsonl"
    predictions = _predictions(predictions_path)
    summary: dict[str, list] = {"completed": [], "skipped": [], "pending": [], "errors": []}
    for task in selected:
        task_id = task["instance_id"]
        if task_id in predictions:
            summary["skipped"].append(task_id)
            continue
        task_dir = output_dir / "tasks" / task_id
        try:
            result_file = task_dir / "result.json"
            attempt_file = task_dir / "attempt.json"
            result = _read_json(result_file) if result_file.exists() else None
            continuing = attempt_file.exists()
            finished = not _unfinished(result)
            if attempt_file.exists() and not finished and not resume:
                summary["pending"].append(task_id)
                continue
            patch_file = task_dir / "patch.diff"
            if not finished or not patch_file.exists():
                container_id = await _container(task, task_dir, manifest["network"])
                environment = ContainerEnvironment(container_id)
            if not finished:
                if not attempt_file.exists():
                    _write_json(attempt_file, {"task_id": task_id, "container_id": container_id})
                result = await run_task(
                    instruction_for(task),
                    environment,
                    settings,
                    task_dir,
                    task_id=task_id,
                    timeout_seconds=timeout_seconds,
                    resume=resume and continuing,
                )
                _write_json(result_file, result)
            if _unfinished(result):
                summary["pending"].append(task_id)
                continue
            if patch_file.exists():
                patch = patch_file.read_text(encoding="utf-8")
            else:
                patch = await collect_patch(environment, task["base_commit"])
                patch_file.write_text(patch, encoding="utf-8", newline="\n")
            prediction = {
                "instance_id": task_id,
                "model_name_or_path": f"yuki/{settings.llm_model}",
                "model_patch": patch,
            }
            with predictions_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(prediction, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            predictions[task_id] = prediction
            summary["completed"].append(task_id)
        except Exception as error:
            details = {"instance_id": task_id, "error": str(error)}
            _write_json(task_dir / "orchestration-error.json", details)
            summary["errors"].append(details)
    _write_json(output_dir / "run-summary.json", summary)
    return summary


async def cleanup_run(output_dir: Path) -> list[str]:
    """Remove only this run's finalized task containers; keep unfinished attempts."""
    output_dir = Path(output_dir)
    removed = []
    for task_id in _predictions(output_dir / "predictions.jsonl"):
        state_file = output_dir / "tasks" / task_id / "environment.json"
        if not state_file.exists():
            continue
        state = _read_json(state_file)
        if state.get("removed"):
            continue
        container_id = state["container_id"]
        container = json.loads(await _docker("container", "inspect", container_id))[0]
        labels = container["Config"]["Labels"] or {}
        if labels.get("io.yuki.benchmark") != "swebench" or (
            labels.get("io.yuki.benchmark.task") != task_id
        ):
            raise ValueError(f"Container {container_id} is not owned by task {task_id}")
        await _docker("container", "rm", "--force", container_id)
        state["removed"] = True
        _write_json(state_file, state)
        removed.append(task_id)
    return removed


def evaluation_command(
    output_dir: Path,
    *,
    run_id: str,
    workers: int = 1,
    timeout_seconds: int = 1800,
) -> list[str]:
    manifest = _read_json(Path(output_dir) / "manifest.json")
    # A local dataset file preserves the exact HF revision used for inference.
    # It stays in the host evaluation directory and is never mounted in a task.
    evaluation_dir = Path(output_dir) / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    dataset_file = evaluation_dir / "dataset.json"
    return [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(dataset_file.resolve()),
        "--split",
        manifest["split"],
        "--predictions_path",
        str((Path(output_dir) / "predictions.jsonl").resolve()),
        "--instance_ids",
        *[task["instance_id"] for task in manifest["tasks"]],
        "--max_workers",
        str(workers),
        "--run_id",
        run_id,
        "--timeout",
        str(timeout_seconds),
    ]


def evaluate(output_dir: Path, *, run_id: str, workers: int, timeout_seconds: int) -> int:
    from datasets import load_dataset

    manifest = _read_json(Path(output_dir) / "manifest.json")
    records = load_dataset(
        manifest["dataset"], split=manifest["split"], revision=manifest["revision"]
    )
    wanted = {task["instance_id"] for task in manifest["tasks"]}
    command = evaluation_command(
        output_dir, run_id=run_id, workers=workers, timeout_seconds=timeout_seconds
    )
    pinned_records = []
    for record in records:
        task_id = record["instance_id"]
        if task_id not in wanted:
            continue
        record = dict(record)
        image_file = Path(output_dir) / "tasks" / task_id / "image.json"
        if image_file.exists():
            record["image"] = _read_json(image_file)["image_id"]
        pinned_records.append(record)
    _write_json(Path(output_dir) / "evaluation" / "dataset.json", pinned_records)
    return_code = subprocess.run(
        command, cwd=Path(output_dir) / "evaluation", check=False
    ).returncode
    if return_code == 0:
        asyncio.run(cleanup_run(output_dir))
    return return_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Pin selected task metadata; no model calls")
    prepare.add_argument("--dataset", default=DEFAULT_DATASET)
    prepare.add_argument("--split", default="dev")
    prepare.add_argument("--revision")
    prepare.add_argument("--network", default="bridge")
    prepare.add_argument("--pull", action="store_true")
    run = commands.add_parser("run", help="Run or resume Yuki in each original task container")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument(
        "--resume", action="store_true", help="Continue an unfinished original attempt"
    )
    evaluation = commands.add_parser("eval", help="Evaluate patches with the official harness")
    evaluation.add_argument("--run-id", required=True)
    evaluation.add_argument("--workers", type=int, default=1)
    evaluation.add_argument("--timeout-seconds", type=int, default=1800)
    cleanup = commands.add_parser(
        "cleanup", help="Remove only finalized task containers owned here"
    )
    for command in (prepare, run, evaluation, cleanup):
        command.add_argument("--output", type=Path, required=True)
    for command in (prepare, run):
        command.add_argument("--instance-id", action="append", dest="instance_ids")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        manifest = asyncio.run(
            prepare_run(
                args.output,
                dataset=args.dataset,
                split=args.split,
                revision=args.revision,
                instance_ids=args.instance_ids,
                pull=args.pull,
                network=args.network,
            )
        )
        print(json.dumps({"tasks": len(manifest["tasks"]), "revision": manifest["revision"]}))
        return 0
    if args.command == "run":
        summary = asyncio.run(
            run_benchmark(
                args.output,
                config_path=args.config,
                instance_ids=args.instance_ids,
                timeout_seconds=args.timeout_seconds,
                resume=args.resume,
            )
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["errors"] or summary["pending"] else 0
    if args.command == "cleanup":
        print(json.dumps({"removed": asyncio.run(cleanup_run(args.output))}))
        return 0
    return evaluate(
        args.output,
        run_id=args.run_id,
        workers=args.workers,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
