"""Run the ACP peer without writing human output into its protocol stream."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import cast

from acp import Agent, run_agent

from yuki.acp_adapter.agent import YukiAgent
from yuki.http_environment import load_http_environment


def add_acp_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("acp", help="本地 ACP stdio 适配器")
    parser.add_argument("--url", default=os.getenv("YUKI_CLIENT_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token-env", default="YUKI_CLIENT_TOKEN")
    parser.add_argument("--entry-binding", default=os.getenv("YUKI_CLIENT_ENTRY_BINDING"))
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("YUKI_ACP_STATE", str(Path.home() / ".yuki-acp"))),
    )


async def _run(args: argparse.Namespace) -> None:
    token = os.getenv(args.token_env, "")
    if not token:
        raise ValueError("device_token_missing")
    agent = YukiAgent(args.url, token, args.state_dir, entry_binding_id=args.entry_binding)
    try:
        # The SDK Protocol includes optional methods; only advertised methods exist here.
        await run_agent(cast(Agent, agent))
    finally:
        await agent.close()


def run_acp_command(args: argparse.Namespace) -> int:
    logging.basicConfig(stream=sys.stderr)
    try:
        load_http_environment()
        asyncio.run(_run(args))
    except (ValueError, OSError) as exc:
        print(f"ACP startup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0
