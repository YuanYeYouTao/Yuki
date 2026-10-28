"""Headless installation, explicit pairing, profiles and user service lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import logging.handlers
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx

from . import PROTOCOL_VERSION, __version__
from .config import Config, Instance, data_path, private_path
from .connector import Connector, installation_lock
from .discovery import candidates, diagnose, refresh
from .drivers import make_driver
from .journal import Journal
from .service import UserService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Connect local native agents to Yuki")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--data-dir", type=Path, default=data_path())
    result.add_argument("--config", type=Path)
    commands = result.add_subparsers(dest="action", required=True)
    pair = commands.add_parser("pair", help="Save an explicitly issued connector device credential")
    pair.add_argument("--url", required=True, help="Existing Yuki https:// address")
    pair.add_argument("--label", required=True, help="Name of this computer")
    pair.add_argument("--code-stdin", action="store_true", help="Read one-time code from stdin")
    profile = commands.add_parser("instance", help="Manage explicit native installs/profiles")
    profiles = profile.add_subparsers(dest="operation", required=True)
    profiles.add_parser("list")
    profiles.add_parser("configure", help="Add/update a native instance from one JSON stdin object")
    add = profiles.add_parser("add")
    add.add_argument("--id", default=None)
    add.add_argument("--label", required=True)
    add.add_argument("--driver", choices=["codex", "claude", "acp"], required=True)
    add.add_argument("--command", required=True, help="Installed native executable")
    add.add_argument(
        "--cwd", type=Path, default=Path.home(), help="Optional default task directory"
    )
    add.add_argument("--socket", help="Existing Codex daemon control socket")
    add.add_argument("--endpoint", help="Existing Codex owner WebSocket endpoint")
    add.add_argument("--agent-arg", action="append", default=[])
    add.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    remove = profiles.add_parser("remove")
    remove.add_argument("id")
    doctor = commands.add_parser("doctor", help="Readonly local native discovery, no model request")
    doctor.add_argument("--instance")
    status = commands.add_parser("status", help="Local configuration and user service status")
    status.add_argument("--json", action="store_true", help="Print one machine-readable object")
    discover = commands.add_parser(
        "discover", help="Read installed native agent locations without starting them"
    )
    discover.add_argument("--json", action="store_true")
    discover.add_argument("--instance", help="Resolve one original instance's installed executable")
    discover.add_argument("--driver", choices=["codex", "claude"], default="codex")
    diagnostic = commands.add_parser("diagnose", help="Explicit native handshake; no model task")
    diagnostic.add_argument("--instance", required=True)
    commands.add_parser("run", help="Run independently of any Desktop UI")
    logs = commands.add_parser("logs")
    logs.add_argument("--lines", type=int, default=60)
    service = commands.add_parser("service")
    service.add_argument("operation", choices=["install", "start", "stop", "status", "uninstall"])
    return result


async def doctor(config: Config, identity: str | None) -> list[dict[str, object]]:
    async def discard(kind: str, payload: dict[str, object]) -> None:
        pass

    results = []
    for spec in config.instances:
        if identity and identity != spec.id:
            continue
        driver = make_driver(spec, discard)
        try:
            await driver.start()
            result = (
                await driver.execute(
                    {
                        "type": "command",
                        "id": str(uuid4()),
                        "action": "list",
                        "instance_id": spec.id,
                        "arguments": {"limit": 1},
                    }
                )
                if "list" in driver.capabilities
                else {"sessions": [], "directory_available": False}
            )
            results.append(
                {
                    "id": spec.id,
                    "available": True,
                    "capabilities": driver.capabilities,
                    "directory": result,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": spec.id,
                    "available": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        finally:
            await driver.stop()
    return results


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    directory = args.data_dir.resolve()
    path = (args.config or directory / "config.json").resolve()
    service = UserService(path, directory)
    try:
        if args.action == "discover":
            discovered: object
            if args.instance:
                instance_config = Config.load(path)
                original = next(
                    (item for item in instance_config.instances if item.id == args.instance), None
                )
                if original is None:
                    raise ValueError("The original instance does not exist")
                discovered = {"instance_id": original.id, "command": refresh(original).command}
            else:
                discovered = candidates() if args.driver == "codex" else candidates(args.driver)
            print(json.dumps(discovered, ensure_ascii=True))
        elif args.action == "pair":
            if path.exists():
                raise ValueError(
                    "Already paired; use a separate --data-dir for another device identity"
                )
            from urllib.parse import urlsplit

            parsed = urlsplit(args.url)
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("Remote pairing requires your Yuki https:// address")
            code = (
                sys.stdin.readline().strip()
                if args.code_stdin
                else (
                    os.environ.get("YUKI_CONNECTOR_CODE")
                    or getpass.getpass("One-time connector code: ")
                )
            )
            base = args.url.rstrip("/")
            response = httpx.post(
                base + "/agents/v1/pair",
                json={
                    "code": code,
                    "device_label": args.label,
                },
            )
            response.raise_for_status()
            credential = response.json()
            target = ("wss" if parsed.scheme == "https" else "ws") + "://" + parsed.netloc
            target += parsed.path.rstrip("/") + "/agents/v1/connect"
            configured = Config(target, credential["device_id"], credential["token"])
            configured.save(path)
            print(f"Paired configuration saved: {path}")
        elif args.action == "service":
            operation = getattr(service, args.operation)
            output = operation()
            print(output or service.describe())
        elif args.action == "logs":
            import collections

            with (directory / "connector.log").open(encoding="utf-8") as stream:
                print("".join(collections.deque(stream, maxlen=args.lines)), end="")
        elif args.action == "status":
            config = Config.load(path) if path.exists() else None
            value = {
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "runtime": sys.executable,
                "data_directory": str(directory),
                "paired": config is not None,
                "service": service.state(),
                "instances": [
                    {
                        "id": item.id,
                        "label": item.label,
                        "driver": item.driver,
                        "command": item.command,
                        "cwd": item.cwd,
                        "args": item.args,
                    }
                    for item in config.instances
                ]
                if config
                else [],
            }
            if config:
                value["device_id"] = config.device_id
                value["server"] = (
                    config.url.removesuffix("/agents/v1/connect")
                    .replace("wss://", "https://", 1)
                    .replace("ws://", "http://", 1)
                )
            print(json.dumps(value, ensure_ascii=args.json, indent=None if args.json else 2))
        else:
            config = Config.load(path)
            if args.action == "instance":
                if args.operation == "list":
                    # Deliberately omit environment, tokens and owner endpoints.
                    print(
                        json.dumps(
                            [
                                {"id": item.id, "label": item.label, "driver": item.driver}
                                for item in config.instances
                            ],
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 0
                if args.operation == "configure":
                    value = json.load(sys.stdin.buffer)
                    identity = value.get("id") or str(uuid4())
                    previous = next((i for i in config.instances if i.id == identity), None)
                    if value.get("id") and previous is None:
                        raise ValueError("The original instance does not exist")
                    candidate_id = value.get("candidateId")
                    driver = previous.driver if previous else value.get("driver", "codex")
                    if driver not in {"codex", "claude", "acp"}:
                        raise ValueError("Choose a supported native driver")
                    candidate = (
                        next(
                            (
                                c
                                for c in (candidates() if driver == "codex" else candidates(driver))
                                if c["id"] == candidate_id
                            ),
                            None,
                        )
                        if candidate_id
                        else None
                    )
                    if candidate_id and candidate is None:
                        raise ValueError("The selected installation is no longer available")
                    command = candidate["command"] if candidate else value.get("command")
                    if (
                        previous is not None
                        and previous.installation
                        and candidate is None
                        and command == previous.command
                    ):
                        previous = refresh(previous)
                        command = previous.command
                    if (
                        not command
                        or not Path(command).is_absolute()
                        or not Path(command).is_file()
                    ):
                        raise ValueError("Choose an installed native executable")
                    if sys.platform == "win32" and Path(command).suffix.lower() != ".exe":
                        raise ValueError("Choose the native executable, not a shell script")
                    original = previous or Instance(identity, driver, "", command)
                    cwd = original.cwd
                    if "cwd" in value:
                        directory = Path(value["cwd"])
                        if not directory.is_absolute() or not directory.is_dir():
                            raise ValueError("Choose an existing task directory")
                        cwd = str(directory)
                    arguments = value.get("args", original.args)
                    if not isinstance(arguments, list) or not all(
                        isinstance(argument, str) for argument in arguments
                    ):
                        raise ValueError("Native arguments must be a list of strings")
                    spec = replace(
                        original,
                        label=value["label"],
                        command=command,
                        cwd=cwd,
                        args=arguments,
                        installation=(
                            candidate["source"]
                            if candidate
                            else previous.installation
                            if previous and command == previous.command
                            else None
                        ),
                    )
                    config = replace(
                        config,
                        instances=[spec if i.id == identity else i for i in config.instances]
                        + ([] if previous else [spec]),
                    )
                elif args.operation == "add":
                    environment = dict(item.split("=", 1) for item in args.env)
                    identity = args.id or str(uuid4())
                    if any(item.id == identity for item in config.instances):
                        raise ValueError(
                            "Instance already exists; remove explicitly before replacing it"
                        )
                    spec = Instance(
                        identity,
                        args.driver,
                        args.label,
                        args.command,
                        args.agent_arg,
                        str(args.cwd.resolve()),
                        environment,
                        args.socket,
                        args.endpoint,
                    )
                    config = replace(config, instances=[*config.instances, spec])
                else:
                    config = replace(
                        config, instances=[i for i in config.instances if i.id != args.id]
                    )
                config.save(path)
                if args.operation == "configure":
                    print(json.dumps({"instance_id": identity}))
                else:
                    print("Configuration updated; restart the connector service to apply it")
            elif args.action == "doctor":
                print(
                    json.dumps(
                        asyncio.run(doctor(config, args.instance)), ensure_ascii=False, indent=2
                    )
                )
            elif args.action == "diagnose":
                selected = next((i for i in config.instances if i.id == args.instance), None)
                if selected is None:
                    raise ValueError("The original instance does not exist")
                print(json.dumps(asyncio.run(diagnose(selected)), ensure_ascii=True))
            elif args.action == "run":
                directory.mkdir(parents=True, exist_ok=True)
                private_path(directory, directory=True)
                handler = logging.handlers.RotatingFileHandler(
                    directory / "connector.log",
                    maxBytes=4 * 1024 * 1024,
                    backupCount=2,
                    encoding="utf-8",
                )
                logging.basicConfig(level=logging.INFO, handlers=[handler])
                private_path(directory / "connector.log")
                with installation_lock(directory / "connector.lock"):
                    journal = Journal(
                        directory / "journal.sqlite3",
                        namespace=json.dumps([config.url, config.device_id]),
                    )
                    try:
                        asyncio.run(Connector(config, journal).run())
                    finally:
                        journal.close()
        return 0
    except (OSError, ValueError, httpx.HTTPError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            problem = (
                "connector_authorization_failed"
                if exc.response.status_code in {401, 403}
                else "connector_server_error"
            )
        elif isinstance(exc, httpx.RequestError):
            problem = "connector_server_unreachable"
        elif isinstance(exc, FileNotFoundError):
            problem = "native_not_installed"
        else:
            problem = "connector_configuration_failed"
        print(json.dumps({"problem": problem}), file=sys.stdout)
        print(f"Connector: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
