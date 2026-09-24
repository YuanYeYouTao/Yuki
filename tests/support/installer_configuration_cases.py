"""Run configuration wrappers against a recording Docker stub, never a real daemon."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def verify_existing_configuration_only(repository: Path, root: Path) -> None:
    if os.name != "posix":
        return  # Both scripts are parsed/checked separately; live wrapper checks run in Linux CI.
    import pty

    deployment = root / "existing deployment"
    deployment.mkdir()
    preserved = {
        "docker-compose.yml": b"custom deployment mounts\n",
        ".env.example": b"example\n",
        ".env": b"EXISTING_CONFIG=keep\n",
        "data/qq_ai_bot.db": b"existing database must not be opened by wrapper",
        "plugins/custom.py": b"existing plugin",
    }
    for name, content in preserved.items():
        path = deployment / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    binaries = root / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_CALL_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args == ['compose', 'version']:
    print('Docker Compose version v2.test')
elif args and args[0] == 'info':
    print('amd64' if 'Architecture' in ' '.join(args) else 'linux')
elif args == ['pull', 'ghcr.io/yuanyeyoutao/yuki-qqbot:3.8.4']:
    pass
elif args and args[0] == 'run' and args[-3:] == ['setup', '--deployment-root', '/deploy']:
    sys.exit(int(os.environ['SETUP_EXIT']))
else:
    raise SystemExit('unexpected Docker mutation: ' + repr(args))
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    for name in ("curl", "wget"):
        command = binaries / name
        command.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        command.chmod(0o755)
    commands = [["sh", str(repository / "install.sh"), "--dir", str(deployment)]]
    if powershell := shutil.which("pwsh"):
        commands.append(
            [
                powershell,
                "-NoProfile",
                "-File",
                str(repository / "install.ps1"),
                "-InstallDir",
                str(deployment),
            ]
        )
    for index, command in enumerate(commands):
        for setup_exit in (0, 23):
            log = root / f"docker-{index}-{setup_exit}.jsonl"
            environment = {
                **os.environ,
                "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "DOCKER_CALL_LOG": str(log),
                "SETUP_EXIT": str(setup_exit),
            }
            master, slave = pty.openpty()
            try:
                result = subprocess.run(
                    command,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=environment,
                    cwd=root,
                    timeout=25,
                    check=False,
                )
            finally:
                os.close(slave)
                os.close(master)
            assert (result.returncode == 0) == (setup_exit == 0), (command, setup_exit)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            assert sum(args[0] == "run" for args in calls) == 1
            assert all(args[0] in {"compose", "info", "pull", "run"} for args in calls)
            assert [args for args in calls if args[0] == "compose"] == [["compose", "version"]]
            assert {
                path.relative_to(deployment).as_posix(): path.read_bytes()
                for path in deployment.rglob("*")
                if path.is_file()
            } == preserved
