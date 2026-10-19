"""Environment-side computer CLI. Uses only its own runtime mount and stdlib."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def wait(root: Path, kind: str, note: str | None) -> dict[str, Any]:
    identity = str(UUID(os.environ["YUKI_JOB_ID"]))
    result = root / "results" / (identity + ".json")
    # A prior registration may already have finished, including before restart
    # or before this process first entered its file wait.
    if not result.exists():
        atomic_json(root / "requests" / (identity + ".json"), {"kind": kind, "note": note})
    while True:
        try:
            return cast(dict[str, Any], json.loads(result.read_text()))
        except FileNotFoundError:
            time.sleep(0.1)


CDP_ADDRESS = "http://127.0.0.1:9222"


def operation(root: Path, method: str, args: dict[str, Any]) -> dict[str, Any]:
    key = str(uuid4())
    atomic_json(
        root / "commands" / (key + ".json"),
        {
            "method": method,
            "args": args,
            "run_id": os.environ.get("YUKI_JOB_ID"),
        },
    )
    result = root / "command-results" / (key + ".json")
    while True:
        try:
            value = cast(dict[str, Any], json.loads(result.read_text()))
            result.unlink(missing_ok=True)
            return value
        except FileNotFoundError:
            time.sleep(0.1)


@contextmanager
def input_lease(root: Path) -> Iterator[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "input.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = operation(root, "agent_acquire", {})
        if result.get("error"):
            raise ComputerError(result)
        try:
            yield result["control"]
        finally:
            operation(root, "agent_release", {"lease_id": result["control"]["lease_id"]})


class ComputerError(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(result.get("error", "computer_error"))


class Browser:
    def __init__(self) -> None:
        self.root = Path.home() / ".local/share/yuki-computer"
        self.root.mkdir(parents=True, exist_ok=True)
        self.before_input: Callable[[], None] | None = None

    def http(self, path: str) -> Any:
        # CDP is the browser on container loopback, never an outbound proxy call.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            CDP_ADDRESS + path
        ) as response:
            return json.load(response)

    def call(self, method: str, params: dict[str, Any], tab: str | None = None) -> dict[str, Any]:
        from websockets.sync.client import connect

        if self.before_input and method in {
            "Target.createTarget",
            "Target.closeTarget",
            "DOM.focus",
            "Input.insertText",
            "Input.dispatchKeyEvent",
            "Input.dispatchMouseEvent",
        }:
            self.before_input()
        url = (
            self.http("/json/version")["webSocketDebuggerUrl"]
            if tab is None
            else next(
                value["webSocketDebuggerUrl"]
                for value in self.http("/json/list")
                if value["id"] == tab
            )
        )
        with connect(url, proxy=None, max_size=None) as socket:
            socket.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                result = json.loads(socket.recv())
                if result.get("id") == 1:
                    if "error" in result:
                        raise ComputerError({"error": "cdp_error", "details": result["error"]})
                    return cast(dict[str, Any], result.get("result", {}))

    @contextmanager
    def records(self) -> Iterator[dict[str, Any]]:
        with (self.root / "tabs.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / "tabs.json"
            try:
                records = json.loads(path.read_text())
            except FileNotFoundError:
                records = {}
            yield records
            atomic_json(path, records)

    def tabs(self) -> list[dict[str, Any]]:
        pages = [tab for tab in self.http("/json/list") if tab.get("type") == "page"]
        with self.records() as records:
            live = {tab["id"] for tab in pages}
            for target in list(records):
                if target not in live:
                    records.pop(target)
            return [
                {
                    "target_id": tab["id"],
                    "title": tab.get("title", ""),
                    "url": tab.get("url", ""),
                    **records.get(tab["id"], {}),
                }
                for tab in pages
            ]

    def target(self, name: str | None) -> str:
        tabs = self.tabs()
        if name and any(tab["target_id"] == name for tab in tabs):
            return name
        candidates = [
            tab
            for tab in tabs
            if tab.get("work_id") == os.environ.get("YUKI_WORK_ID")
            and (name is None or tab.get("name") == name)
        ]
        if len(candidates) != 1:
            raise ComputerError(
                {"error": "tab_not_found" if not candidates else "tab_ambiguous", "tabs": tabs}
            )
        return cast(str, candidates[0]["target_id"])

    def open(self, url: str, name: str | None) -> dict[str, Any]:
        target = self.call("Target.createTarget", {"url": url})["targetId"]
        with self.records() as records:
            records[target] = {
                "work_id": os.environ.get("YUKI_WORK_ID"),
                "created_at": time.time(),
                "name": name,
            }
        return {"target_id": target}

    def close(self, target: str) -> dict[str, Any]:
        result = self.call("Target.closeTarget", {"targetId": target})
        with self.records() as records:
            records.pop(target, None)
        return result

    def snapshot(self, target: str) -> dict[str, Any]:
        nodes = self.call("Accessibility.getFullAXTree", {}, target).get("nodes", [])
        return {
            "target_id": target,
            "text": "\n".join(
                f"[{node.get('backendDOMNodeId', '')}] "
                f"{node.get('role', {}).get('value', '')}: "
                f"{node.get('name', {}).get('value', '')}"
                for node in nodes
                if not node.get("ignored")
            ),
        }

    def click(self, target: str, node: int) -> dict[str, Any]:
        # Remote object handles belong to the CDP session that created them.
        # Evaluate a resolved backend node through one persistent connection.
        return self.node_call(
            target, node, "function(){this.scrollIntoView({block:'center'});this.click()}"
        )

    def node_call(self, target: str, node: int, function: str) -> dict[str, Any]:
        from websockets.sync.client import connect

        url = next(
            value["webSocketDebuggerUrl"]
            for value in self.http("/json/list")
            if value["id"] == target
        )
        with connect(url, proxy=None, max_size=None) as socket:

            def call(identity: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
                socket.send(json.dumps({"id": identity, "method": method, "params": params}))
                while True:
                    result = json.loads(socket.recv())
                    if result.get("id") == identity:
                        if result.get("error"):
                            raise ComputerError({"error": "cdp_error", "details": result["error"]})
                        return cast(dict[str, Any], result["result"])

            if self.before_input:
                self.before_input()
            resolved = call(1, "DOM.resolveNode", {"backendNodeId": node})
            if self.before_input:
                self.before_input()
            return call(
                2,
                "Runtime.callFunctionOn",
                {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": function,
                    "returnByValue": True,
                },
            )


def mark(label: str) -> None:
    if os.environ.get("YUKI_WORK_ID") and os.environ.get("YUKI_JOB_ID"):
        path = (
            Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer")).parent
            / str(UUID(os.environ["YUKI_JOB_ID"]))
            / "state/step-label.json"
        )
        atomic_json(path, {"step_label": label})


def main() -> int:
    parser = argparse.ArgumentParser(prog="yuki-computer")
    commands = parser.add_subparsers(dest="command", required=True)
    help_request = commands.add_parser("help-request")
    help_request.add_argument("--note", required=True)
    commands.add_parser("wait-control")
    for command in ("wake", "sleep", "status"):
        commands.add_parser(command).add_argument("--json", action="store_true")
    screenshot = commands.add_parser("screenshot")
    screenshot.add_argument("--tab")
    screenshot.add_argument("--screen", action="store_true")
    screenshot.add_argument("--out", required=True)
    browser = commands.add_parser("browser").add_subparsers(dest="action", required=True)
    for command in ("open", "snapshot", "close", "click", "type", "press", "scroll"):
        entry = browser.add_parser(command)
        entry.add_argument("--tab")
        if command in ("open", "type", "press"):
            entry.add_argument("value")
        if command in ("click", "type"):
            entry.add_argument("--node", type=int, required=command == "click")
        if command == "scroll":
            for field in ("x", "y", "delta-x", "delta-y"):
                entry.add_argument("--" + field, type=float, default=0)
    inputs = commands.add_parser("input").add_subparsers(dest="action", required=True)
    click = inputs.add_parser("click")
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    for command in ("type", "key"):
        inputs.add_parser(command).add_argument("value")
    args = parser.parse_args()
    root = Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer"))
    try:
        if args.command in ("help-request", "wait-control"):
            value = wait(
                root,
                "help" if args.command == "help-request" else "wait_control",
                getattr(args, "note", None),
            )
        elif args.command in ("wake", "sleep"):
            value = operation(root, "display_" + args.command, {})
        elif args.command == "status":
            value = {
                "control": json.loads((root / "control.json").read_text()),
                "tabs": Browser().tabs(),
            }
        elif args.command == "browser" and args.action == "snapshot":
            mark("browser")
            client = Browser()
            value = client.snapshot(client.target(args.tab))
        elif args.command == "screenshot":
            mark("computer" if args.screen else "browser")
            if args.screen:
                subprocess.run(
                    ["import", "-display", ":1", "-window", "root", args.out], check=True
                )
            else:
                client = Browser()
                image = client.call(
                    "Page.captureScreenshot", {"format": "png"}, client.target(args.tab)
                )
                Path(args.out).write_bytes(base64.b64decode(image["data"]))
            value = {"path": args.out}
        else:
            mark("browser" if args.command == "browser" else "computer")
            with input_lease(root) as lease:
                if args.command == "input":
                    call = (
                        ["xdotool", "mousemove", "--sync", str(args.x), str(args.y), "click", "1"]
                        if args.action == "click"
                        else [
                            "xdotool",
                            "type" if args.action == "type" else "key",
                            "--clearmodifiers",
                            "--",
                            args.value,
                        ]
                    )
                    subprocess.run(call, check=True, env={**os.environ, "DISPLAY": ":1"})
                    value = {"submitted": True}
                else:
                    client = Browser()

                    def still_held() -> None:
                        current = json.loads((root / "control.json").read_text())
                        if current.get("lease_id") != lease["lease_id"]:
                            raise ComputerError({"error": "control_held", "control": current})

                    client.before_input = still_held
                    if args.action == "open":
                        value = client.open(args.value, args.tab)
                    else:
                        target = client.target(args.tab)
                        if args.action == "snapshot":
                            value = client.snapshot(target)
                        elif args.action == "close":
                            value = client.close(target)
                        elif args.action == "click":
                            value = client.click(target, args.node)
                        elif args.action == "type":
                            if args.node is not None:
                                client.call("DOM.focus", {"backendNodeId": args.node}, target)
                            value = client.call("Input.insertText", {"text": args.value}, target)
                        elif args.action == "press":
                            parts = args.value.split("+")
                            modifiers = sum(
                                {"Alt": 1, "Ctrl": 2, "Meta": 4, "Shift": 8}[part]
                                for part in parts[:-1]
                            )
                            params = {"key": parts[-1], "modifiers": modifiers}
                            client.call(
                                "Input.dispatchKeyEvent", {"type": "keyDown", **params}, target
                            )
                            value = client.call(
                                "Input.dispatchKeyEvent", {"type": "keyUp", **params}, target
                            )
                        else:
                            value = client.call(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": "mouseWheel",
                                    "x": args.x,
                                    "y": args.y,
                                    "deltaX": args.delta_x,
                                    "deltaY": args.delta_y,
                                },
                                target,
                            )
        print(json.dumps(value, ensure_ascii=False))
        return 75 if value.get("error") == "control_held" else (1 if value.get("error") else 0)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.CalledProcessError) as error:
        print(
            json.dumps({"error": "computer_unavailable", "detail": str(error)}, ensure_ascii=False)
        )
        return 1
    except ComputerError as error:
        print(json.dumps(error.result, ensure_ascii=False))
        return 75 if error.result.get("error") == "control_held" else 1


if __name__ == "__main__":
    raise SystemExit(main())
