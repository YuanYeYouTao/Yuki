"""Her computer end to end: snapshots, restore, rebuild, autostart and idle sleep.

Stages of ``scripts/sandbox_compose_e2e.py`` against the real installer, Docker engine,
Manager and environment (task book docs/architecture/Yuki自己的电脑任务书-2026-11-14.md
§12). Every call goes through the Manager socket as the Bot's user. Each stage writes
``snapshots-<profile>.json``: every request, receipt, ``docker image ls`` of snapshot
images, the snapshot volume, sizes and pause seconds.

- lite: A1–A5 (``/etc/rc.local``), S1, S6, S7, S9, S13, S14, S21, R11.
- headless: Chromium profile and cookies restored with HOME, S15, S16, S22 (restore to a
  snapshot from ``lite``), R6, R10.
- desktop: her session's autostart restored with the computer (screenshots), an open
  display stream keeps the screen awake, idle sleep after it closes (R1, R2).
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from scripts.sandbox_compose_e2e import Deployment

SNAPSHOT_IMAGES = (
    "docker",
    "image",
    "ls",
    "--filter",
    "label=io.yuki.snapshot",
    "--format",
    "{{.Repository}}:{{.Tag}} {{.Size}}",
)
HOLD = """
import json, socket, sys, time
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect("/run/yuki-sandbox/manager.sock")
    connection.sendall(b'{"method":"display_stream","args":{}}\\n')
    header = b""
    while not header.endswith(b"\\n"):
        header += connection.recv(1)
    print(header.decode().strip(), connection.recv(12).decode(errors="replace"), flush=True)
    sys.stdin.read()
"""
COOKIE_PAGE = (
    "<!doctype html><title>cookie</title><script>"
    "const v=new URLSearchParams(location.search).get('v');"
    "if(v){document.cookie='yuki='+v+'; max-age=86400; path=/'}"
    "document.title='cookie:'+document.cookie</script>"
)


class Failure(AssertionError):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


class Computer:
    """One profile's stage: requests through the Manager, facts from Docker."""

    def __init__(self, deployment: Deployment, profile: str) -> None:
        self.deployment, self.profile = deployment, profile
        self.steps: list[dict[str, Any]] = []

    def record(self, name: str, **facts: Any) -> None:
        self.steps.append({"step": name, "at": time.time(), **facts})
        path = self.deployment.artifacts / f"snapshots-{self.profile}.json"
        path.write_text(json.dumps(self.steps, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"ok  {self.profile}: {name}")

    def computer(self, action: str, key: str | None = None, **args: Any) -> dict[str, Any]:
        return self.deployment.manager(
            "computer_action", {"action": action, **args}, key or str(uuid4())
        )

    def browser(self, action: str, **args: Any) -> dict[str, Any]:
        return self.deployment.manager("browser_action", {"action": action, **args})

    def shell(self, command: str) -> str:
        result = self.deployment.terminal(command)
        expect(result["status"] == "succeeded", f"{command}: {result}")
        return str(result["output"])

    def images(self) -> list[str]:
        return self.deployment.run(*SNAPSHOT_IMAGES).split("\n")[:-1]

    def volume(self) -> dict[str, Any]:
        listing = self.deployment.run(
            "docker",
            "run",
            "--rm",
            "-v",
            "yuki-sandbox-snapshots:/s:ro",
            "--entrypoint",
            "sh",
            f"ghcr.io/yuanyeyoutao/yuki:{self.deployment.version}",
            "-c",
            "ls /s; du -sk /s | cut -f1",
        ).split()
        return {"entries": listing[:-1], "kib": int(listing[-1])}

    def container(self) -> dict[str, Any]:
        return json.loads(self.deployment.run("docker", "inspect", "yuki-environment"))[0]  # type: ignore[no-any-return]

    def snapshot(self, note: str, key: str | None = None) -> dict[str, Any]:
        taken = self.computer("snapshot", key, text=note)
        expect(taken.get("snapshot", {}).get("state") == "ready", f"snapshot {note}: {taken}")
        return taken["snapshot"]  # type: ignore[no-any-return]

    def restore(self, snapshot_id: str, key: str | None = None) -> dict[str, Any]:
        done = self.computer("restore", key, snapshot=snapshot_id)
        expect(done.get("restore", {}).get("state") == "done", f"restore: {done}")
        self.deployment.wait_ready()
        return done["restore"]  # type: ignore[no-any-return]

    def settings(self, **values: str) -> None:
        """Product settings in .env; the Manager is recreated, the environment is not."""
        path = self.deployment.directory / ".env"
        lines = [
            line for line in path.read_text().splitlines() if line.split("=", 1)[0] not in values
        ]
        lines += [f"{key}={value}" for key, value in values.items()]
        path.write_text("\n".join(lines) + "\n")
        self.deployment.compose("up", "-d", "manager")
        self.deployment.wait_ready()

    def wait(self, check: Any, seconds: float, message: str) -> Any:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(3)
        raise Failure(message)


def autostart(stage: Computer) -> None:
    """A1–A5: /etc/rc.local once per environment generation, never per Manager start."""
    status = stage.deployment.status()
    log = stage.shell("cat /var/log/rc.local.log 2>/dev/null; echo end")
    expect(log.strip().endswith("end") and "exit" not in log, f"A1 ran a missing rc.local: {log}")
    stage.shell(
        "printf '%s\\n' 'date +%s.%N >> /root/rc-runs' 'sleep 7200 &' "
        "'echo started-daemon' 'exit 7' > /etc/rc.local"
    )
    stage.deployment.run("docker", "restart", "yuki-environment")
    stage.wait(
        lambda: (
            stage.deployment.status().get("generation") not in {None, status["generation"]}
            and stage.deployment.status().get("ready")
        ),
        120,
        "the Manager did not follow the environment restart",
    )
    runs = stage.wait(
        lambda: (
            stage.shell("cat /root/rc-runs 2>/dev/null | wc -l").strip() == "1"
            and stage.shell("tail -n1 /var/log/rc.local.log").strip()
        ),
        60,
        "rc.local did not run after the environment restart",
    )
    expect(runs == "exit 7", f"A2: the real exit code is not the last line: {runs}")
    daemon = stage.shell("ps -eo args | grep -c '^sleep 7200' || true").strip()
    expect(daemon == "1", f"A3: the rc.local daemon is not running: {daemon}")
    stage.deployment.compose("restart", "manager")
    stage.deployment.wait_ready()
    time.sleep(5)
    again = stage.shell("cat /root/rc-runs | wc -l").strip()
    expect(again == "1", f"A4: a Manager restart ran rc.local again: {again}")
    stage.record(
        "autostart_once_per_generation",
        log=stage.shell("cat /var/log/rc.local.log"),
        runs=again,
        daemon=daemon,
    )


def lite(deployment: Deployment) -> dict[str, Any]:
    stage = Computer(deployment, "lite")
    stage.settings(
        YUKI_SANDBOX_SNAPSHOT_KEEP="3",
        YUKI_SANDBOX_SNAPSHOT_INTERVAL_HOURS="0",
        YUKI_SANDBOX_DISPLAY_IDLE_MINUTES="0.5",
    )
    status = deployment.status()
    expect(status["snapshot_keep"] == 3, f"settings not applied: {status}")
    stage.record("settings", snapshots=status["snapshots"], images=stage.images())
    # R11: lite has no screen or browser to wake or sleep.
    for action in ("wake", "sleep"):
        refused = stage.computer(action)
        expect(refused.get("error") == "browser_unavailable", f"lite {action}: {refused}")
    autostart(stage)

    # S1/S14/S21: snapshot, change, a running command, restore, retry.
    stage.shell(
        "apt-get update -qq && apt-get install -y -qq --no-install-recommends tree >/dev/null "
        "&& echo lite-a > /etc/yuki-marker && echo home-a > /workspace/marker.txt"
    )
    runs_at_snapshot = stage.shell("cat /root/rc-runs | wc -l").strip()
    before = stage.snapshot("lite before", "lite-snapshot-a")
    again = stage.computer("snapshot", "lite-snapshot-a", text="lite before")
    expect(again["snapshot"]["id"] == before["id"], f"S1 snapshot retry: {again}")
    stage.shell(
        "echo lite-b > /etc/yuki-marker && echo home-b > /workspace/marker.txt "
        "&& touch /workspace/after.txt && apt-get remove -y -qq tree >/dev/null"
    )
    long = deployment.manager("terminal_exec", {"command": "sleep 600"})
    generation = deployment.status()["generation"]
    restored = stage.restore(before["id"], "lite-restore-a")
    status = deployment.status()
    expect(status["generation"] != generation, "the restore kept the old environment")
    checks = stage.shell(
        "cat /etc/yuki-marker /workspace/marker.txt; test -e /workspace/after.txt "
        "&& echo after-present || echo after-absent; command -v tree; cat /root/rc-runs | wc -l"
    ).split()
    expect(checks[:3] == ["lite-a", "home-a", "after-absent"], f"restored files: {checks}")
    expect(checks[3].endswith("/tree"), f"the snapshot's software is missing: {checks}")
    expect(checks[4] == str(int(runs_at_snapshot) + 1), f"A5 after restore: {checks}")
    interrupted = deployment.manager("terminal_read", {"run_id": long["run_id"]})
    expect(
        interrupted["status"] == "failed" and interrupted.get("error") == "environment_restored",
        f"S14 the running command: {interrupted}",
    )
    retry = stage.computer("restore", "lite-restore-a", snapshot=before["id"])
    expect(
        retry["restore"]["generation"] == restored["generation"]
        and deployment.status()["generation"] == status["generation"],
        f"S1 restore retry: {retry}",
    )
    container = stage.container()
    expect(not container["State"]["OOMKilled"], "S21 a snapshot step ran out of memory")
    stage.record(
        "snapshot_restore_retry",
        snapshot=before,
        restore=restored,
        interrupted=interrupted,
        checks=checks,
        images=stage.images(),
        volume=stage.volume(),
    )

    # S6/S7: rotation to three; the restored container's base stays tagged until rebuilt.
    taken = []
    for index in range(3):
        stage.shell(f"echo rotation-{index} > /workspace/rotation.txt")
        taken.append(stage.snapshot(f"rotation {index}"))
    ready = [item for item in deployment.status()["snapshots"] if item["state"] == "ready"]
    expect(len(ready) == 3, f"S6 kept {len(ready)}: {ready}")
    rotated = taken[-1]["rotated"]
    images = stage.images()
    expect(
        any(before["image"] in line for line in images),
        f"S7 the image the container runs on was removed: {images}",
    )
    volume = stage.volume()
    expect(
        not {entry for entry in volume["entries"]} - {item["id"] for item in ready},
        f"S6 rotated HOME copies remain: {volume}",
    )
    stage.record("rotation", taken=taken, rotated=rotated, images=images, volume=volume)

    # S13: the container is lost; HOME stays current, the rootFS is the latest snapshot.
    latest = taken[-1]
    stage.shell("echo after-latest > /etc/after-latest && echo home-current > /workspace/kept.txt")
    generation = deployment.status()["generation"]
    deployment.run("docker", "rm", "-f", "yuki-environment")
    status = stage.wait(
        lambda: (
            (current := deployment.status()).get("ready")
            and current["generation"] != generation
            and current
        ),
        180,
        "the lost environment was not rebuilt",
    )
    expect(status["recreated_from"]["snapshot_id"] == latest["id"], f"S13: {status}")
    rebuilt = stage.shell(
        "test -e /etc/after-latest && echo root-kept || echo root-snapshot; cat /workspace/kept.txt"
    ).split()
    expect(rebuilt == ["root-snapshot", "home-current"], f"S13 contents: {rebuilt}")
    images = stage.images()
    expect(
        not any(before["image"] in line for line in images),
        f"S7 the rotated base image stayed after the rebuild: {images}",
    )
    stage.record("rebuilt_from_latest", recreated_from=status["recreated_from"], images=images)

    # S9: the daily check takes nothing without a change, and one after a change.
    stage.settings(YUKI_SANDBOX_SNAPSHOT_INTERVAL_HOURS="0.004")
    count = len(deployment.status()["snapshots"])
    time.sleep(40)
    unchanged = deployment.status()["snapshots"]
    expect(len(unchanged) == count, f"S9 a daily snapshot without a change: {unchanged}")
    stage.shell("echo changed > /workspace/daily.txt")
    daily = stage.wait(
        lambda: [
            item
            for item in deployment.status()["snapshots"]
            if item["source"] == "daily" and item["created_at"] > time.time() - 120
        ],
        90,
        "S9 no daily snapshot after a change",
    )
    stage.settings(YUKI_SANDBOX_SNAPSHOT_INTERVAL_HOURS="0")
    stage.record("daily_only_after_change", unchanged=len(unchanged), daily=daily)
    # The newest snapshot of this profile, for S22 after the move to headless.
    newest = max(
        (item for item in deployment.status()["snapshots"] if item["state"] == "ready"),
        key=lambda item: item["point_at"],
    )
    return {"lite_snapshot": newest}


def wait_cookie(stage: Computer, value: str) -> None:
    """Chromium writes cookies lazily; wait until the profile on disk has this one."""

    def written() -> bool:
        output = stage.shell(
            "cp ~/.local/share/yuki-computer/chromium/Default/Cookies /tmp/c.db 2>/dev/null "
            "&& python3 -c \"import sqlite3; print([r[0] for r in sqlite3.connect('/tmp/c.db')"
            '.execute(\\"SELECT value FROM cookies WHERE name=\'yuki\'\\")])" || echo none'
        )
        return f"'{value}'" in output

    stage.wait(written, 120, f"cookie {value} never reached the profile on disk")


def headless(deployment: Deployment, earlier: dict[str, Any]) -> None:
    stage = Computer(deployment, "headless")
    for action in ("sleep", "wake"):
        result = stage.computer(action)
        expect(not result.get("error"), f"headless {action}: {result}")
    stage.shell(
        "mkdir -p /workspace/site && printf '%s' "
        + json.dumps(COOKIE_PAGE)
        + " > /workspace/site/index.html && (cd /workspace/site && "
        "nohup python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 &)"
    )
    opened = stage.browser("open", url="http://127.0.0.1:8765/?v=v1")
    expect(not opened.get("error"), f"open: {opened}")
    old_tab = opened["tab"]["id"]
    wait_cookie(stage, "v1")
    # S16: taken while Chromium runs, so the copy carries its SingletonLock.
    lock = stage.shell("readlink ~/.local/share/yuki-computer/chromium/SingletonLock || true")
    with_browser = stage.snapshot("browser running")
    stage.browser("open", url="http://127.0.0.1:8765/?v=v2", tab=old_tab)
    wait_cookie(stage, "v2")
    stage.restore(with_browser["id"])
    stage.shell(
        "(cd /workspace/site && nohup python3 -m http.server 8765 --bind 127.0.0.1 "
        ">/dev/null 2>&1 &)"
    )
    stale = stage.browser("snapshot", tab=old_tab)
    expect(stale.get("error") == "tab_not_found" and "tabs" in stale, f"S15 old tab: {stale}")
    page = stage.browser("open", url="http://127.0.0.1:8765/")
    shown = json.dumps(page, ensure_ascii=False)
    expect("cookie:yuki=v1" in shown, f"the restored profile's cookie: {shown[:2000]}")
    stage.record(
        "chromium_profile_restored",
        lock=lock.strip(),
        snapshot=with_browser,
        page=page,
        stale_tab=stale,
    )

    # R10/R6: idle sleep stops the headless browser; the next browser call wakes it.
    tab = page["tab"]["id"]
    stage.wait(lambda: deployment.status().get("browser") == "stopped", 150, "R10 no idle sleep")
    woken = stage.browser("snapshot", tab=tab)
    expect(woken.get("error") == "tab_not_found", f"R6 old tab after sleep: {woken}")
    expect(deployment.status()["browser"] == "running", "R6 the browser did not wake")
    stage.record("idle_sleep_and_wake", stale_after_sleep=woken)

    # S22: back to the lite-era snapshot; the current profile installs what it needs.
    stage.restore(earlier["lite_snapshot"]["id"])
    status = deployment.wait_ready(1800)
    expect(status["browser_mode"] == "headless", f"S22 the profile follows .env: {status}")
    after = stage.browser("open", url="data:text/html,<h1>after-s22</h1>")
    expect(not after.get("error"), f"S22 browser after restoring lite: {after}")
    stage.record(
        "restore_lite_snapshot_in_headless",
        programs=status["programs"],
        images=stage.images(),
        volume=stage.volume(),
    )


def desktop(deployment: Deployment) -> None:
    stage = Computer(deployment, "desktop")
    stage.shell(
        "mkdir -p ~/.config/openbox && printf '%s\\n' "
        "'date +%s.%N >> $HOME/session-autostart' "
        "\"xmessage -geometry +60+60 'her desktop' &\" > ~/.config/openbox/autostart"
    )
    stage.computer("sleep")
    woken = stage.computer("wake")
    expect(woken.get("display") == "running", f"desktop wake: {woken}")
    stage.wait(lambda: "her desktop" in stage.shell("ps -eo args"), 60, "no autostart window")
    shots = deployment.artifacts / "desktop"
    shots.mkdir(exist_ok=True)

    def screenshot(name: str) -> str:
        shot = stage.computer("screenshot", max_dimension=1280, max_pixels=1_000_000)
        path = shots / f"{name}.png"
        path.write_bytes(base64.b64decode(shot["image"]["base64"]))
        return path.name

    first = screenshot("session-autostart")
    taken = stage.snapshot("her desktop")
    stage.shell("rm ~/.config/openbox/autostart")
    stage.restore(taken["id"])
    stage.computer("wake")
    stage.wait(
        lambda: "her desktop" in stage.shell("ps -eo args"), 60, "restored autostart did not run"
    )
    restored = screenshot("restored-session-autostart")
    stage.record("session_restored_with_computer", snapshot=taken, shots=[first, restored])

    # R2/R1: an open display stream keeps the screen awake; it sleeps after it closes.
    hold = subprocess.Popen(
        ["docker", "compose", "exec", "-T", "--user", "10001:10001", "bot", "python", "-c", HOLD],
        cwd=deployment.directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert hold.stdout is not None and hold.stdin is not None
    banner = hold.stdout.readline()
    expect("ready" in banner and "RFB" in banner, f"display stream: {banner}")
    expect(deployment.status()["display_streams"] == 1, "the stream was not counted")
    time.sleep(50)
    expect(deployment.status()["display"] == "running", "R2 slept with a stream open")
    hold.stdin.close()
    hold.wait(30)
    stage.wait(lambda: deployment.status()["display"] == "stopped", 150, "R1 no idle sleep")
    gone = stage.shell("ps -eo args | grep -c 'her desktop' || true").strip()
    expect(gone == "0", f"R5 her X client outlived the display: {gone}")
    stage.record(
        "stream_keeps_awake_then_idle_sleep",
        banner=banner.strip(),
        images=stage.images(),
        volume=stage.volume(),
        status=deployment.status(),
    )
