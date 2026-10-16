"""Finite management CLI; credentials and secret values stay out of arguments."""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from qq_ai_bot.admin_api.client import admin_client, identity_mutation, mutate, read
from qq_ai_bot.config import Settings


def _mutation(parser: argparse.ArgumentParser, *, revision: bool = True) -> None:
    parser.add_argument("--request-id")
    if revision:
        parser.add_argument("--revision", type=int, required=True)


def add_admin_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    admin = subparsers.add_parser("admin", help="通过 Bearer API 管理运行中的 Yuki")
    commands = admin.add_subparsers(dest="admin_command", required=True)
    commands.add_parser("health")
    operation = commands.add_parser("operation")
    operation.add_argument("id", help="原 operation ID 或本 operator 的 request UUID")
    trace = commands.add_parser("trace")
    trace.add_argument("kind", choices=("turn", "work", "event"))
    trace.add_argument("id")
    trace.add_argument("--content", action="store_true")
    trace.add_argument("--after", type=int, default=0)
    trace.add_argument("--limit", type=int, default=100)
    person = commands.add_parser("person").add_subparsers(dest="person_action", required=True)
    created = person.add_parser("create")
    created.add_argument("--display-name", default="")
    _mutation(created, revision=False)
    shown = person.add_parser("show")
    shown.add_argument("id")
    disabled = person.add_parser("disable")
    disabled.add_argument("id")
    _mutation(disabled)
    forgotten = person.add_parser("forget")
    forgotten.add_argument("id")
    _mutation(forgotten, revision=False)
    merged = person.add_parser("merge")
    merged.add_argument("id")
    merged.add_argument("--to-person", required=True)
    _mutation(merged, revision=False)
    renamed = person.add_parser("rename")
    renamed.add_argument("id")
    renamed.add_argument("display_name")
    _mutation(renamed, revision=False)
    personal = commands.add_parser("client").add_subparsers(dest="client_action", required=True)
    pair = personal.add_parser("pair")
    pair.add_argument("--person", required=True)
    _mutation(pair, revision=False)
    revoke = personal.add_parser("revoke")
    revoke.add_argument("id")
    _mutation(revoke, revision=False)
    binding = commands.add_parser("binding").add_subparsers(dest="binding_action", required=True)
    attach = binding.add_parser("attach")
    attach.add_argument("--person", required=True)
    attach.add_argument("--platform", required=True)
    attach.add_argument("--account", required=True)
    attach.add_argument("--display-name", default="")
    _mutation(attach)
    shown = binding.add_parser("show")
    shown.add_argument("id")
    disabled = binding.add_parser("disable")
    disabled.add_argument("id")
    _mutation(disabled)
    transfer = binding.add_parser("transfer")
    transfer.add_argument("id")
    transfer.add_argument("--to-person", required=True)
    _mutation(transfer, revision=False)
    model = commands.add_parser("model").add_subparsers(dest="model_action", required=True)
    model.add_parser("show")
    apply = model.add_parser("apply")
    apply.add_argument("file", type=Path, help="model_profiles 的 TOML 或 JSON document")
    _mutation(apply)
    secret = model.add_parser("secret").add_subparsers(dest="secret_action", required=True)
    setting = secret.add_parser("set")
    setting.add_argument("name")
    setting.add_argument("--from-env", required=True, help="从指定环境变量读取新密钥")
    _mutation(setting)
    work = commands.add_parser("work").add_subparsers(dest="work_action", required=True)
    for action in ("cancel", "resume"):
        parser = work.add_parser(action)
        parser.add_argument("id")
        _mutation(parser)


async def admin_command(settings: Settings, args: argparse.Namespace) -> int:
    async with admin_client(settings) as client:
        kind = args.admin_command
        data: Any
        if kind == "health":
            data = await read(client, "health")
        elif kind == "operation":
            data = await read(client, f"operations/{args.id}")
        elif kind == "trace":
            data = await read(
                client,
                f"traces/{args.kind}/{args.id}",
                content=args.content,
                after=args.after,
                limit=args.limit,
            )
        elif kind == "person":
            if args.person_action == "show":
                data = await read(client, f"persons/{args.id}")
            elif args.person_action in {"merge", "rename"}:
                values = (
                    {"to_person": args.to_person}
                    if args.person_action == "merge"
                    else {"display_name": args.display_name}
                )
                data = await identity_mutation(
                    client,
                    f"persons/{args.id}/{args.person_action}",
                    request_id=args.request_id,
                    **values,
                )
            elif args.person_action == "forget":
                data = await identity_mutation(
                    client, f"persons/{args.id}/forget", request_id=args.request_id
                )
            else:
                path = "persons" if args.person_action == "create" else f"persons/{args.id}/disable"
                fields = (
                    {"display_name": args.display_name}
                    if args.person_action == "create" and args.display_name
                    else {}
                )
                data = await mutate(
                    client,
                    path,
                    getattr(args, "revision", 0),
                    request_id=args.request_id,
                    **fields,
                )
        elif kind == "binding":
            if args.binding_action == "show":
                data = await read(client, f"bindings/{args.id}")
            elif args.binding_action == "attach":
                data = await mutate(
                    client,
                    f"persons/{args.person}/bindings",
                    args.revision,
                    request_id=args.request_id,
                    platform=args.platform,
                    external_account_id=args.account,
                    display_name=args.display_name,
                )
            elif args.binding_action == "transfer":
                data = await identity_mutation(
                    client,
                    f"bindings/{args.id}/transfer",
                    request_id=args.request_id,
                    to_person=args.to_person,
                )
            else:
                data = await mutate(
                    client, f"bindings/{args.id}/disable", args.revision, request_id=args.request_id
                )
        elif kind == "client":
            path = (
                f"persons/{args.person}/client-pair"
                if args.client_action == "pair"
                else f"devices/{args.id}/revoke"
            )
            data = await identity_mutation(client, path, request_id=args.request_id)
        elif kind == "model":
            if args.model_action == "show":
                data = await read(client, "models")
            elif args.model_action == "apply":
                raw = args.file.read_text(encoding="utf-8")
                document = tomllib.loads(raw) if args.file.suffix == ".toml" else json.loads(raw)
                data = await mutate(
                    client,
                    "models/apply",
                    args.revision,
                    request_id=args.request_id,
                    document=document,
                )
            else:
                data = await mutate(
                    client,
                    "models/secrets",
                    args.revision,
                    request_id=args.request_id,
                    name=args.name,
                    value=os.environ[args.from_env],
                )
        else:
            data = await mutate(
                client,
                f"works/{args.id}/{args.work_action}",
                args.revision,
                request_id=args.request_id,
            )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
