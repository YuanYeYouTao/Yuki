"""Small process-external personal CLI; device credentials come from environment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from yuki.client_sdk import ClientProblem, Outbox, PersonalClient


def add_client_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("client", help="个人 HTTP 客户端（持久请求与重连核对）")
    parser.add_argument("--url", default=os.getenv("YUKI_CLIENT_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token-env", default="YUKI_CLIENT_TOKEN")
    parser.add_argument("--entry-binding", default=os.getenv("YUKI_CLIENT_ENTRY_BINDING"))
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(
            os.getenv("YUKI_CLIENT_STATE", str(Path.home() / ".yuki-client" / "outbox.sqlite3"))
        ),
    )
    commands = parser.add_subparsers(dest="client_action", required=True)
    schema = commands.add_parser("openapi", help="导出当前程序实际实现的个人 API 合同")
    schema.add_argument("--output", type=Path, required=True)
    pair = commands.add_parser("pair")
    pair.add_argument("--code-env", default="YUKI_CLIENT_CODE")
    pair.add_argument("--label", required=True)
    for name in ("me", "home", "watch", "pending", "link-code"):
        commands.add_parser(name)
    message = commands.add_parser("send")
    message.add_argument("text")
    message.add_argument("--attachment", action="append", default=[])
    message.add_argument("--request-id")
    message.add_argument("--draft", action="store_true")
    inp = commands.add_parser("input")
    inp.add_argument("work_id")
    inp.add_argument("text")
    inp.add_argument("--request-id")
    inp.add_argument("--draft", action="store_true")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("work_id")
    cancel.add_argument("--revision", type=int, required=True)
    cancel.add_argument("--request-id")
    upload = commands.add_parser("upload")
    upload.add_argument("file", type=Path)
    upload.add_argument("--kind", choices=("file", "image", "video", "record"), default="file")
    upload.add_argument("--request-id")
    for name in ("retry", "request", "work", "revoke"):
        commands.add_parser(name).add_argument("id")
    events = commands.add_parser("events")
    events.add_argument("conversation_id")
    events.add_argument("--after", type=int, default=0)
    artifact = commands.add_parser("artifact")
    artifact.add_argument("id")
    artifact.add_argument("--version", required=True)
    artifact.add_argument("--output", type=Path, required=True)


async def client_command(args: argparse.Namespace) -> int:
    if args.client_action == "openapi":
        from yuki.client_api.openapi import export_openapi

        export_openapi(args.output)
        return 0
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        if args.client_action == "pair":
            code = os.getenv(args.code_env, "")
            if not code:
                raise ValueError("pairing_code_missing")
            response = await http.post(
                f"{args.url.rstrip('/')}/client/v1/pair",
                json={"code": code, "device_label": args.label},
            )
            print(json.dumps(PersonalClient.data(response), ensure_ascii=False))
            return 0
        token = os.getenv(args.token_env, "")
        if not token:
            raise ValueError("device_token_missing")
        client = PersonalClient(
            args.url, token, Outbox(args.state), http=http, entry_binding_id=args.entry_binding
        )
        await client._bind()
        action = args.client_action
        data: Any = None
        if action in {"me", "home"}:
            data = await client.get(action)
        elif action == "events":
            data = await client.get(
                f"conversations/{args.conversation_id}/events", after=args.after
            )
        elif action == "work":
            data = await client.get(f"works/{args.id}")
        elif action == "request":
            data = await client.get(f"requests/{args.id}")
        elif action == "pending":
            data = client.outbox.pending()
        elif action == "watch":
            async for snapshot in client.watch():
                print(json.dumps(snapshot, ensure_ascii=False), flush=True)
            return 0
        elif action == "artifact":
            response = await http.get(
                client.endpoint(f"artifacts/{args.id}"),
                headers=client.headers,
                params={"version": args.version},
            )
            if response.is_error:
                client.data(response)
            # The server already authorized the original snapshot; verify bytes before saving.
            import hashlib

            if hashlib.sha256(response.content).hexdigest() != args.version:
                raise ValueError("artifact_integrity_failed")
            args.output.write_bytes(response.content)
            data = {"output": str(args.output), "sha256": args.version}
        elif action in {"link-code", "revoke"}:
            identity = str(uuid4())
            try:
                response = await http.request(
                    "POST" if action == "link-code" else "DELETE",
                    client.endpoint(
                        "link-codes" if action == "link-code" else f"devices/{args.id}"
                    ),
                    headers=client.headers,
                    json={"request_id": identity},
                )
            except httpx.TransportError:
                print(json.dumps({"problem": "transport_unknown", "request_id": identity}))
                return 2
            data = client.data(response)
        else:
            if action == "send":
                identity = await client.prepare_message(
                    args.text, tuple(args.attachment), request_id=args.request_id
                )
            elif action == "input":
                identity = await client.prepare_input(
                    args.work_id, args.text, request_id=args.request_id
                )
            elif action == "cancel":
                identity = await client.prepare_cancel(
                    args.work_id, args.revision, request_id=args.request_id
                )
            elif action == "upload":
                identity = await client.prepare_upload(
                    args.file, args.kind, request_id=args.request_id
                )
            else:
                identity = args.id
            if getattr(args, "draft", False):
                data = client.outbox.get(identity)
            else:
                try:
                    data = await client.submit(identity)
                except ClientProblem as exc:
                    exc.details = {"request_id": identity, **exc.details}
                    raise
                except httpx.TransportError:
                    print(
                        json.dumps(
                            {
                                "problem": "transport_unknown",
                                "request_id": identity,
                                "state": client.outbox.get(identity)["state"],
                            }
                        )
                    )
                    return 2
        print(json.dumps(data, ensure_ascii=False))
    return 0


def run_client_command(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(client_command(args))
    except ClientProblem as exc:
        print(json.dumps({"problem": {"code": exc.code, **exc.details}}, ensure_ascii=False))
        return 1
    except (ValueError, KeyError) as exc:
        print(json.dumps({"problem": str(exc)}, ensure_ascii=False))
        return 1
    except httpx.TransportError:
        print(
            json.dumps(
                {"problem": "transport_unknown", "next": "query or retry the original request_id"}
            )
        )
        return 2
