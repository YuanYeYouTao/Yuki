"""Explicit management CLI over the running Bot's operator API."""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from yuki.admin_api.client import (
    admin_client,
    character_mutation,
    identity_mutation,
    mutate,
    read,
    soul_mutation,
)
from yuki.config import Settings
from yuki.model_runtime.semantic_observation import (
    PROVIDER_IDS,
    SemanticConfigError,
    semantic_use_document,
)


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
    character = commands.add_parser("character").add_subparsers(
        dest="character_action", required=True
    )
    character.add_parser("show")
    updated = character.add_parser("update")
    updated.add_argument("--display-name")
    updated.add_argument("--slug")
    updated.add_argument("--bio")
    _mutation(updated)
    soul = character.add_parser("soul").add_subparsers(dest="soul_action", required=True)
    soul.add_parser("show")
    saved = soul.add_parser("set")
    saved.add_argument("file", type=Path)
    saved.add_argument("--revision")
    _mutation(saved, revision=False)
    _mutation(soul.add_parser("reload"), revision=False)
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
    pair.add_argument("--purpose", choices=("chat", "connector"), default="chat")
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
    migration = model.add_parser("migrate-legacy", help="离线迁移既有模型来源；不启动 Bot")
    migration.add_argument("--revision", type=int)
    apply = model.add_parser("apply")
    apply.add_argument("file", type=Path)
    _mutation(apply)
    secret = model.add_parser("secret").add_subparsers(dest="secret_action", required=True)
    setting = secret.add_parser("set")
    setting.add_argument("name")
    setting.add_argument("--from-env", required=True)
    _mutation(setting)
    semantic = model.add_parser("semantic").add_subparsers(dest="semantic_action", required=True)
    use = semantic.add_parser("use")
    use.add_argument("--provider", required=True, choices=PROVIDER_IDS)
    use.add_argument("--name")
    use.add_argument("--endpoint")
    use.add_argument("--request-model", required=True)
    use.add_argument("--response-model")
    use.add_argument("--timeout", type=float)
    use.add_argument("--key-env")
    use.add_argument("--from-env")
    _mutation(use)
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
        elif kind == "character":
            if args.character_action == "show":
                data = await read(client, "character")
            elif args.character_action == "update":
                data = await character_mutation(
                    client,
                    request_id=args.request_id,
                    expected_revision=args.revision,
                    **{
                        key: getattr(args, key)
                        for key in ("display_name", "slug", "bio")
                        if getattr(args, key) is not None
                    },
                )
            elif args.soul_action == "show":
                data = await read(client, "character/soul")
            else:
                data = await soul_mutation(
                    client,
                    request_id=args.request_id,
                    content=args.file.read_bytes().decode("utf-8")
                    if args.soul_action == "set"
                    else None,
                    expected_revision=getattr(args, "revision", None),
                )
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
            fields = {"purpose": args.purpose} if args.client_action == "pair" else {}
            data = await identity_mutation(client, path, request_id=args.request_id, **fields)
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
            elif args.model_action == "semantic":
                shown = await read(client, "models")
                try:
                    document, key_env = semantic_use_document(
                        shown["document"],
                        provider=args.provider,
                        request_model=args.request_model,
                        name=args.name,
                        endpoint=args.endpoint,
                        response_model=args.response_model,
                        timeout_seconds=args.timeout,
                        key_env=args.key_env,
                    )
                except SemanticConfigError as exc:
                    raise SystemExit(exc.category) from None
                semantic_fields: dict[str, Any] = {"document": document}
                if args.from_env:
                    semantic_fields["api_keys"] = {key_env: os.environ[args.from_env]}
                data = await mutate(
                    client,
                    "models/apply",
                    args.revision,
                    request_id=args.request_id,
                    **semantic_fields,
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
