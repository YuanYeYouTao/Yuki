"""Real-machine acceptance of Computer Use (task book Computer-Use-2026-11-15 §12).

Drives an acceptance Bot (its own deployment and data, never production) through the
personal Client API exactly as a person would: one instruction per run in a fresh chat,
then checks the facts on the machine itself, read-only, and compares them with what the
model said. Success rates are recorded only; they are never a gate.

    python scripts/computer_use_acceptance.py run --task T1 --target mac --mode fg \\
        --model claude --runs 5 --label branch
    python scripts/computer_use_acceptance.py summary

Targets: ``mac`` (the owner's Mac through the test Connector, ``--device``) and ``own``
(Yuki's own computer in the acceptance deployment). Facts: Finder's window names (T1 on
the Mac), the two TextEdit files on disk (T2), ``xprop`` window names (T1 on her computer).
Each run writes ``run-XX/run.json`` (instruction, Yuki's replies, the Work, the trace with
every tool call and receipt, model usage, facts, verdict) plus the step screenshots with the
echoed point drawn in; ``summary.json`` aggregates all runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / ".cache" / "yuki-cu-acceptance"
DEPLOYMENT = STATE / "deployment"
SCENE = Path.home() / "Yuki-验收"
URL = "http://127.0.0.1:18080"
DOCKER = {**os.environ, "DOCKER_CONTEXT": "colima-yuki-cu-acc"}
RUN_SECONDS = 600
VERDICT = re.compile(r"结果[:：]\s*(成功|失败)")
TASKS = {
    ("T1", "mac"): "关掉显示 Yuki-关闭 的 Finder 窗口，另一个（Yuki-保留）不要动。",
    ("T2", "mac"): "TextEdit 里开着两个文档。只在标题为 Yuki-目标 的那个文档末尾另起一行写上"
    "「{line}」并保存（cmd+s），另一个文档不要改。",
    ("T1", "own"): "你自己的电脑桌面上有两个窗口，标题分别是 Yuki-保留 和 Yuki-关闭。"
    "关掉 Yuki-关闭 那个窗口，另一个不要动。",
}
MODES = {
    "fg": "请在前台操作整个屏幕（调用 computer 时不要给 window）。",
    "bg": "请按窗口在后台操作（先截图，调用 computer 时给出目标窗口的 window），"
    "不要动我的鼠标和前台应用。",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def run(*command: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{shlex.join(command)} failed: {result.stdout}{result.stderr}")
    return result.stdout


def client(*arguments: str) -> Any:
    token = (STATE / "client-token").read_text().strip()
    output = run(
        "uv",
        "run",
        "--no-sync",
        "yuki",
        "client",
        "--url",
        URL,
        "--token-env",
        "YUKI_ACCEPTANCE_TOKEN",
        "--state",
        str(STATE / "client-state.json"),
        *arguments,
        env={**os.environ, "YUKI_ACCEPTANCE_TOKEN": token},
    )
    return json.loads(output)


def bot(*command: str) -> str:
    """A command inside the acceptance Bot container, as its operator."""
    script = 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" exec "$@"'
    return run(
        "docker",
        "compose",
        "--project-directory",
        str(DEPLOYMENT),
        "exec",
        "-T",
        "bot",
        "sh",
        "-c",
        script,
        "sh",
        *command,
        env=DOCKER,
    )


def environment(command: str) -> str:
    """A shell command in Yuki's own computer, as her user, on her display."""
    return run(
        "docker",
        "exec",
        "-u",
        "yuki",
        "-e",
        "DISPLAY=:1",
        "yuki-environment",
        "sh",
        "-c",
        command,
        env=DOCKER,
        check=False,
    )


def osascript(script: str) -> str:
    return run("osascript", "-e", script, check=False).strip()


# Scenes and facts -------------------------------------------------------------


def scene_mac_t1() -> None:
    for name in ("Yuki-保留", "Yuki-关闭"):
        (SCENE / name).mkdir(parents=True, exist_ok=True)
    osascript(
        'tell application "Finder" to close (every Finder window whose name is "Yuki-保留" '
        'or name is "Yuki-关闭")'
    )
    run("open", str(SCENE / "Yuki-保留"))
    time.sleep(1)
    run("open", str(SCENE / "Yuki-关闭"))
    time.sleep(1.5)


def facts_mac_t1() -> dict[str, Any]:
    names = osascript('tell application "Finder" to get name of every Finder window')
    windows = [name.strip() for name in names.split(",") if name.strip()]
    running = osascript('application "Finder" is running') == "true"
    return {
        "finder_windows": windows,
        "finder_running": running,
        "success": "Yuki-关闭" not in windows and "Yuki-保留" in windows and running,
    }


def scene_mac_t2(line: str) -> dict[str, str]:
    SCENE.mkdir(parents=True, exist_ok=True)
    osascript(
        'tell application "TextEdit" to close (every document whose path contains '
        '"Yuki-验收") saving no'
    )
    original = {
        "Yuki-目标.txt": "这是目标文档。\n",
        "Yuki-其他.txt": "这是另一个文档，不要改。\n",
    }
    for name, text in original.items():
        (SCENE / name).write_text(text, encoding="utf-8")
    for name in original:
        run("open", "-a", "TextEdit", str(SCENE / name))
        time.sleep(1)
    time.sleep(1)
    return {"line": line, **original}


def facts_mac_t2(scene: dict[str, str]) -> dict[str, Any]:
    target = (SCENE / "Yuki-目标.txt").read_text(encoding="utf-8")
    other = (SCENE / "Yuki-其他.txt").read_text(encoding="utf-8")
    last = [line for line in target.splitlines() if line.strip()][-1:]
    return {
        "target": target,
        "other": other,
        "success": last == [scene["line"]]
        and target.startswith(scene["Yuki-目标.txt"].rstrip("\n"))
        and other == scene["Yuki-其他.txt"],
    }


def scene_own_t1() -> None:
    environment("pkill -x xmessage")
    for name in ("Yuki-保留", "Yuki-关闭"):
        environment(f"nohup xmessage -title {name} -name {name} '{name}' >/dev/null 2>&1 &")
    time.sleep(2)


def facts_own_t1() -> dict[str, Any]:
    listed = environment(
        "for w in $(xprop -root _NET_CLIENT_LIST | grep -o '0x[0-9a-f]*'); do "
        "xprop -id $w _NET_WM_NAME WM_NAME | head -1; done"
    )
    names = re.findall(r'"([^"]*)"', listed)
    return {
        "windows": names,
        "success": "Yuki-关闭" not in names and "Yuki-保留" in names,
    }


# One run ----------------------------------------------------------------------


def stats() -> Any:
    return json.loads(bot("yuki", "model", "stats"))


def replies(conversation: str, after: int) -> list[dict[str, Any]]:
    """Yuki's own messages after the instruction (the instruction itself names the verdict)."""
    events = client("events", conversation, "--after", str(after))
    return [item for item in events["items"] if item.get("direction") == "outbound"]


def one_run(args: argparse.Namespace, directory: Path, index: int) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    line = f"Yuki 验收 {args.label} {args.model} {args.mode} {index:02d}"
    scene: dict[str, str] = {}
    if args.target == "mac" and args.task == "T1":
        scene_mac_t1()
    elif args.target == "mac":
        scene = scene_mac_t2(line)
    else:
        scene_own_t1()
    instruction = TASKS[(args.task, args.target)].format(line=line)
    if args.target == "mac":
        instruction = f"在我的电脑（device={args.device}）上：{instruction}{MODES[args.mode]}"
    instruction += "做完后用一句话汇报，并在最后单独一行写「结果：成功」或「结果：失败」。"
    client("send", "/ai new")
    time.sleep(3)
    home = client("home")
    conversation = home["main"]["conversation_id"]
    before_stats = stats()
    started = time.monotonic()
    sent = client("send", instruction)
    event = int(sent["result"]["event_id"])
    seen: list[dict[str, Any]] = []
    verdict = None
    works: list[dict[str, Any]] = []
    while time.monotonic() - started < RUN_SECONDS:
        time.sleep(5)
        seen = replies(conversation, event)
        works = [work for work in client("home").get("works", [])]
        texts = " ".join(json.dumps(item, ensure_ascii=False) for item in seen)
        found = VERDICT.search(texts)
        running = [work for work in works if work.get("status") in {"running", "waiting"}]
        if found and not running:
            verdict = found.group(1)
            break
    elapsed = time.monotonic() - started
    time.sleep(2)
    facts = (
        facts_mac_t1()
        if (args.target, args.task) == ("mac", "T1")
        else facts_mac_t2(scene)
        if args.target == "mac"
        else facts_own_t1()
    )
    traces = {
        str(work["id"]): bot("yuki", "admin", "trace", "work", str(work["id"]), "--content")
        for work in works
        if "id" in work
    }
    record = {
        "at": now(),
        "label": args.label,
        "model": args.model,
        "task": args.task,
        "target": args.target,
        "mode": args.mode,
        "index": index,
        "instruction": instruction,
        "elapsed_seconds": round(elapsed, 1),
        "replies": seen,
        "works": works,
        "usage_before": before_stats,
        "usage_after": stats(),
        "facts": facts,
        "claimed": verdict,
        "success": bool(facts["success"]),
        # A false claim: the model said it succeeded and the facts say otherwise (M3).
        "false_claim": verdict == "成功" and not facts["success"],
        "timed_out": verdict is None,
    }
    (directory / "run.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    for identity, trace in traces.items():
        (directory / f"trace-{identity}.txt").write_text(trace, encoding="utf-8")
    return record


def output_root(args: argparse.Namespace) -> Path:
    return ROOT / "test-results" / "computer-use-acceptance" / args.date


def command_run(args: argparse.Namespace) -> int:
    root = output_root(args)
    target = "mac-mini" if args.target == "mac" else "own-computer"
    cell = root / target / args.label / args.model / args.task / args.mode
    for index in range(1, args.runs + 1):
        record = one_run(args, cell / f"run-{index:02d}", index)
        print(
            json.dumps(
                {
                    key: record[key]
                    for key in ("model", "task", "mode", "index", "success", "claimed")
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


def command_summary(args: argparse.Namespace) -> int:
    root = output_root(args)
    cells: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("**/run.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        key = "/".join(path.relative_to(root).parts[:-2])
        cell = cells.setdefault(
            key, {"runs": 0, "success": 0, "false_claims": 0, "timed_out": 0, "seconds": []}
        )
        cell["runs"] += 1
        cell["success"] += record["success"]
        cell["false_claims"] += record["false_claim"]
        cell["timed_out"] += record["timed_out"]
        cell["seconds"].append(record["elapsed_seconds"])
    (root / "summary.json").write_text(
        json.dumps(cells, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(cells, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    commands = parser.add_subparsers(dest="command", required=True)
    runs = commands.add_parser("run")
    runs.add_argument("--task", choices=["T1", "T2"], required=True)
    runs.add_argument("--target", choices=["mac", "own"], required=True)
    runs.add_argument("--mode", choices=["fg", "bg"], required=True)
    runs.add_argument("--model", required=True, help="the profile name applied for this cell")
    runs.add_argument("--label", default="branch", help="branch or baseline")
    runs.add_argument("--runs", type=int, default=5)
    runs.add_argument("--device", default="")
    commands.add_parser("summary")
    args = parser.parse_args()
    return command_run(args) if args.command == "run" else command_summary(args)


if __name__ == "__main__":
    raise SystemExit(main())
