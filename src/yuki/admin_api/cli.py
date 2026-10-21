"""Explicit management CLI over the running Bot's operator API."""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from yuki.admin_api.client import admin_client, identity_mutation, mutate, read
from yuki.config import Settings


def _mutation(parser: argparse.ArgumentParser, *, revision: bool = True) -> None:
    parser.add_argument("--request-id")
    if revision:
        parser.add_argument("--revision", type=int, required=True)


def _listing(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--after", default="")
    parser.add_argument("--limit", type=int, default=100)


def add_admin_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    admin = subparsers.add_parser("admin", help="通过 operator API 管理正在运行的 Yuki")
    commands = admin.add_subparsers(dest="admin_command", required=True)
    commands.add_parser("health")
    operation = commands.add_parser("operation")
    operation.add_argument("id", help="原 operation ID 或本 operator 的请求 UUID")
    trace = commands.add_parser("trace")
    trace.add_argument("kind", choices=("turn", "work", "event"))
    trace.add_argument("id")
    trace.add_argument("--content", action="store_true")
    trace.add_argument("--after", type=int, default=0)
    trace.add_argument("--limit", type=int, default=100)
    person = commands.add_parser("person").add_subparsers(dest="person_action", required=True)
    _listing(person.add_parser("list"))
    created = person.add_parser("create")
    created.add_argument("--display-name", default="")
    _mutation(created, revision=False)
    shown = person.add_parser("show")
    shown.add_argument("id")
    for action in ("enable", "disable"):
        state = person.add_parser(action)
        state.add_argument("id")
        _mutation(state)
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
    role = person.add_parser("superuser").add_subparsers(dest="superuser_action", required=True)
    for action in ("grant", "revoke"):
        state = role.add_parser(action)
        state.add_argument("id")
        _mutation(state)
    personal = commands.add_parser("client").add_subparsers(dest="client_action", required=True)
    pair = personal.add_parser("pair")
    pair.add_argument("--person", required=True)
    _mutation(pair, revision=False)
    revoke = personal.add_parser("revoke")
    revoke.add_argument("id")
    _mutation(revoke, revision=False)
    binding = commands.add_parser("binding").add_subparsers(dest="binding_action", required=True)
    listing = binding.add_parser("list")
    _listing(listing)
    listing.add_argument("--person")
    listing.add_argument("--platform")
    listing.add_argument("--account")
    attached = binding.add_parser("attach")
    attached.add_argument("--person", required=True)
    attached.add_argument("--platform", required=True)
    attached.add_argument("--account", required=True)
    attached.add_argument("--display-name", default="")
    _mutation(attached)
    shown = binding.add_parser("show")
    shown.add_argument("id")
    for action in ("enable", "disable", "unbind"):
        state = binding.add_parser(action)
        state.add_argument("id")
        _mutation(state)
    transfer = binding.add_parser("transfer")
    transfer.add_argument("id")
    transfer.add_argument("--to-person", required=True)
    _mutation(transfer, revision=False)
    presence = commands.add_parser("presence").add_subparsers(dest="presence_action", required=True)
    _listing(presence.add_parser("list"))
    shown = presence.add_parser("show")
    shown.add_argument("id")
    registered = presence.add_parser("register")
    registered.add_argument("--platform", required=True)
    registered.add_argument("--account", required=True)
    registered.add_argument("--expected-assignment")
    registered.add_argument("--display-name", default="")
    _mutation(registered)
    transferred = presence.add_parser("transfer")
    transferred.add_argument("id")
    transferred.add_argument("--to-person", required=True)
    _mutation(transferred)
    retired = presence.add_parser("unregister")
    retired.add_argument("id")
    _mutation(retired)
    for action in ("enable", "disable", "ingest"):
        state = presence.add_parser(action)
        state.add_argument("id")
        _mutation(state)
        if action == "ingest":
            state.add_argument("state", choices=("on", "off"))
    model = commands.add_parser("model").add_subparsers(dest="model_action", required=True)
    model.add_parser("show")
    apply = model.add_parser("apply")
    apply.add_argument("file", type=Path)
    _mutation(apply)
    secret = model.add_parser("secret").add_subparsers(dest="secret_action", required=True)
    setting = secret.add_parser("set")
    setting.add_argument("name")
    setting.add_argument("--from-env", required=True)
    _mutation(setting)
    work = commands.add_parser("work").add_subparsers(dest="work_action", required=True)
    listing = work.add_parser("list")
    _listing(listing)
    listing.add_argument("--state")
    shown = work.add_parser("show")
    shown.add_argument("id")
    for action in ("cancel", "resume"):
        state = work.add_parser(action)
        state.add_argument("id")
        _mutation(state)


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
                after=args.after,
                limit=args.limit,
                content=args.content,
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
                text = args.file.read_text(encoding="utf-8")
                document = json.loads(text) if args.file.suffix == ".json" else tomllib.loads(text)
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
            action = getattr(args, f"{kind}_action")
            plural = {
                "person": "persons",
                "binding": "bindings",
                "presence": "presences",
                "work": "works",
            }[kind]
            if action == "list":
                params: dict[str, Any] = {"after": args.after, "limit": args.limit}
                if kind == "binding":
                    params.update(
                        {
                            key: value
                            for key, value in {
                                "person_id": args.person,
                                "platform": args.platform,
                                "external_account_id": args.account,
                            }.items()
                            if value is not None
                        }
                    )
                elif kind == "work" and args.state is not None:
                    params["state"] = args.state
                data = await read(client, plural, **params)
            elif action == "show":
                data = await read(client, f"{plural}/{args.id}")
            elif kind == "person" and action == "create":
                data = await mutate(
                    client, "persons", 0, request_id=args.request_id, display_name=args.display_name
                )
            elif kind == "presence" and action == "register":
                data = await mutate(
                    client,
                    "presences",
                    args.revision,
                    request_id=args.request_id,
                    platform=args.platform,
                    external_account_id=args.account,
                    display_name=args.display_name,
                    expected_assignment_id=args.expected_assignment,
                )
            elif kind == "presence" and action == "transfer":
                data = await mutate(
                    client,
                    f"presences/{args.id}/transfer",
                    args.revision,
                    request_id=args.request_id,
                    to_person=args.to_person,
                )
            elif action in {"forget", "merge", "rename", "transfer"}:
                values = (
                    {"to_person": args.to_person}
                    if action in {"merge", "transfer"}
                    else {"display_name": args.display_name}
                    if action == "rename"
                    else {}
                )
                data = await identity_mutation(
                    client, f"{plural}/{args.id}/{action}", request_id=args.request_id, **values
                )
            elif kind == "binding" and action == "attach":
                data = await mutate(
                    client,
                    f"persons/{args.person}/bindings",
                    args.revision,
                    request_id=args.request_id,
                    platform=args.platform,
                    external_account_id=args.account,
                    display_name=args.display_name,
                )
            elif kind == "presence" and action != "unregister":
                values = (
                    {"ingest_eligible": args.state == "on"}
                    if action == "ingest"
                    else {"enabled": action == "enable"}
                )
                data = await mutate(
                    client,
                    f"presences/{args.id}",
                    args.revision,
                    request_id=args.request_id,
                    **values,
                )
            else:
                suffix = f"superuser/{args.superuser_action}" if action == "superuser" else action
                data = await mutate(
                    client,
                    f"{plural}/{args.id}/{suffix}",
                    args.revision,
                    request_id=args.request_id,
                )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
