"""The docker and rsync commands the Manager runs, answered like the engine would.

Used where the Manager's own code runs without a Docker engine: snapshot, restore and
recovery decisions stay real; one container, its images and HOME copies are modelled.
The real engine path is covered by ``scripts/sandbox_snapshot_e2e.py``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
import os
import re
import shutil
import time
from pathlib import Path
from uuid import uuid4


class Crash(BaseException):
    """The Manager process ends at this command (never caught as an ordinary error)."""


def _rule(pattern: str) -> tuple[str, bool, bool]:
    anchored = pattern.startswith("/")
    directory = pattern.endswith("/")
    return pattern.strip("/"), anchored, directory


def _excluded(relative: str, is_dir: bool, rules: list[tuple[str, bool, bool]]) -> bool:
    for pattern, anchored, directory in rules:
        if directory and not is_dir:
            continue
        regex = fnmatch.translate(pattern.replace("**/", "\0")).replace("\0", "(?:.*/)?")
        candidates = [relative] if anchored else [relative.split("/", i)[-1] for i in range(9)]
        if any(re.fullmatch(regex, value) for value in candidates):
            return True
    return False


def rsync(source: Path, target: Path, excludes: list[str], link_dest: Path | None) -> None:
    """``rsync -aH --delete``: mirror ``source``; unchanged files hard link ``link_dest``."""
    rules = [_rule(pattern) for pattern in excludes]
    target.mkdir(parents=True, exist_ok=True)
    wanted: set[str] = set()
    for directory, names, files in os.walk(source):
        relative_dir = os.path.relpath(directory, source)
        relative_dir = "" if relative_dir == "." else relative_dir
        for name in list(names):
            relative = os.path.join(relative_dir, name)
            if _excluded(relative, True, rules):
                names.remove(name)
                continue
            wanted.add(relative)
            (target / relative).mkdir(exist_ok=True)
        for name in files:
            relative = os.path.join(relative_dir, name)
            if _excluded(relative, False, rules):
                continue
            wanted.add(relative)
            origin, destination = source / relative, target / relative
            previous = link_dest / relative if link_dest else None
            if destination.is_symlink() or destination.exists():
                destination.unlink()
            if origin.is_symlink():
                os.symlink(os.readlink(origin), destination)
            elif (
                previous is not None
                and previous.is_file()
                and previous.read_bytes() == origin.read_bytes()
            ):
                os.link(previous, destination)
            else:
                shutil.copy2(origin, destination)
    for directory, names, files in os.walk(target, topdown=False):
        relative_dir = os.path.relpath(directory, target)
        relative_dir = "" if relative_dir == "." else relative_dir
        for name in (*files, *names):
            relative = os.path.join(relative_dir, name)
            if relative in wanted or _excluded(relative, name in names, rules):
                continue
            path = target / relative
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


class FakeDocker:
    def __init__(
        self, *, name: str = "yuki-environment", project: str = "", running: bool = True
    ) -> None:
        self.name, self.project = name, project
        self.images: dict[str, dict] = {"base": {"labels": {}, "rootfs": {}}}
        self.container: dict | None = None
        self.calls: list[tuple[str, ...]] = []
        # Command prefix -> (exit code, output) for one simulated engine failure.
        self.fail: dict[tuple[str, ...], tuple[int, bytes]] = {}
        self.crash: tuple[str, ...] | None = None
        self.delay = 0.0
        if running:
            self.create("base")
            self.start()

    # ---- the modelled environment ----------------------------------------------------

    def create(self, image: str) -> None:
        self.container = {
            "Id": uuid4().hex,
            "image": image,
            "Running": False,
            "Paused": False,
            "StartedAt": "",
            "OOMKilled": False,
            "rootfs": dict(self.images[image]["rootfs"]),
            "changed": time.time(),
        }

    def start(self) -> None:
        assert self.container is not None
        self.container.update(Running=True, StartedAt=f"{time.time():.6f}")

    def write_rootfs(self, path: str, text: str | None) -> None:
        assert self.container is not None and not self.container["Paused"]
        if text is None:
            self.container["rootfs"].pop(path, None)
        else:
            self.container["rootfs"][path] = text
        self.container["changed"] = time.time()

    def stop(self, *, oom: bool = False) -> None:
        assert self.container is not None
        self.container.update(Running=False, Paused=False, OOMKilled=oom)

    def snapshot_tags(self) -> list[str]:
        return sorted(
            tag for tag, image in self.images.items() if "io.yuki.snapshot" in image["labels"]
        )

    def inspect(self) -> dict | None:
        if self.container is None:
            return None
        return {
            "Id": self.container["Id"],
            "Image": self.container["image"],
            "SizeRw": 4096 * len(self.container["rootfs"]),
            "Config": {
                "Image": self.container["image"],
                "Labels": {
                    "io.yuki.sandbox": "persistent-v1",
                    "io.yuki.sandbox.project": self.project,
                },
            },
            "State": {
                "Running": self.container["Running"],
                "Paused": self.container["Paused"],
                "StartedAt": self.container["StartedAt"],
                "OOMKilled": self.container["OOMKilled"],
            },
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "NetworkSettings": {"Networks": {"egress": {"IPAddress": "127.0.0.1"}}},
            "Mounts": [],
        }

    # ---- the CLI -------------------------------------------------------------------

    async def command(self, *args: str, deadline_seconds: float = 30) -> tuple[int, bytes]:
        self.calls.append(args)
        for prefix, answer in self.fail.items():
            if args[: len(prefix)] == prefix:
                return answer
        if self.crash and args[: len(self.crash)] == self.crash:
            self.crash = ("",)  # The process is gone: nothing after this runs either.
        if self.crash == ("",):
            raise Crash(" ".join(args))
        if args[0] == "rsync":
            excludes = [arg.split("=", 1)[1] for arg in args if arg.startswith("--exclude=")]
            link = next(
                (arg.split("=", 1)[1] for arg in args if arg.startswith("--link-dest=")), None
            )
            source, target = Path(args[-2]), Path(args[-1])
            rsync(source, target, excludes, (target / link).resolve() if link else None)
            return 0, b""
        assert args[0] == "docker", args
        verb = args[1:3]
        container = self.container
        if args[1] == "inspect":
            found = self.inspect() if args[-1] == self.name else None
            return (0, json.dumps([found]).encode()) if found else (1, b"No such object")
        if args[1] == "exec":
            if container is None or not container["Running"] or container["Paused"]:
                return 1, b"container is not running or is paused"
            if args[3] == "sync":
                return 0, b""
            # The rootFS scan prints the first entry changed after the given time.
            return 0, b"/etc\n" if container["changed"] > float(args[-1]) else b""
        if args[1] in {"pause", "unpause"}:
            if container is None or not container["Running"]:
                return 1, b"container is not running"
            container["Paused"] = args[1] == "pause"
            return 0, b""
        if args[1] == "commit":
            assert container is not None
            labels = dict(
                value.removeprefix("LABEL ").split("=", 1)
                for flag, value in itertools.pairwise(args)
                if flag == "-c"
            )
            if self.delay:
                await asyncio.sleep(self.delay)
            self.images[args[-1]] = {"labels": labels, "rootfs": dict(container["rootfs"])}
            return 0, b""
        if verb == ("image", "rm"):
            tag = args[3]
            if tag not in self.images:
                return 1, b"No such image"
            if container is not None and container["image"] == tag:
                return 1, b"conflict: unable to delete (must be forced) - container is using it"
            del self.images[tag]
            return 0, b""
        if verb == ("image", "inspect"):
            tag = args[-1]
            if tag not in self.images:
                return 1, b"No such image"
            if "-f" in args:
                return 0, (
                    self.images[tag]["labels"].get("io.yuki.sandbox.project", "") + "\n"
                ).encode()
            return 0, json.dumps([self.images[tag]]).encode()
        if verb == ("image", "ls"):
            return 0, ("\n".join(self.snapshot_tags()) + "\n").encode()
        if args[1] == "stop":
            if container is not None:
                self.stop()
            return 0, b""
        if args[1] == "rm":
            if container is None:
                return 1, b"No such container"
            self.container = None
            return 0, b""
        if args[1] == "create":
            if args[-1] not in self.images:
                return 1, b"Unable to find image"
            self.create(args[-1])
            return 0, b""
        if args[1] == "start":
            assert container is not None
            self.start()
            return 0, b""
        return 0, b""
