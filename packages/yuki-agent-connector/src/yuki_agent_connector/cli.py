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

from . import __version__
from .config import Config, Instance, data_path, private_path
from .connector import Connector, installation_lock
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
    add = profiles.add_parser("add")
    add.add_argument("--id", default=None)
    add.add_argument("--label", required=True)
    add.add_argument("--driver", choices=["codex", "claude", "acp"], required=True)
    add.add_argument("--command", required=True, help="Installed native executable")
    add.add_argument("--cwd", type=Path, required=True)
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
        if args.action == "pair":
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
                "paired": config is not None,
                "service": service.state(),
                "instances": [
                    {"id": item.id, "label": item.label, "driver": item.driver}
                    for item in config.instances
                ]
                if config
                else [],
            }
            if config:
                value["device_id"] = config.device_id
            print(json.dumps(value, ensure_ascii=False, indent=None if args.json else 2))
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
                if args.operation == "add":
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
                print("Configuration updated; restart the connector service to apply it")
            elif args.action == "doctor":
                print(
                    json.dumps(
                        asyncio.run(doctor(config, args.instance)), ensure_ascii=False, indent=2
                    )
                )
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
        print(f"Connector: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
