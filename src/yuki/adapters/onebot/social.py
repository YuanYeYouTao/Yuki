"""QQ wire encoding and gateway filesystem handoff."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

from yuki.adapters.onebot.sender import parse_onebot_send_receipt
from yuki.gateway.social import (
    TransportEffectError,
    TransportMention,
    TransportMessage,
    TransportReceipt,
    TransportTarget,
)


def _write_handoff(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o644)


class OneBotSocialOperations:
    def native_mention(self, text: str, offset: int) -> TransportMention | None:
        for name in ("@全体成员", "@all"):
            if text.startswith(name, offset):
                return TransportMention("all", "全体成员", offset, offset + len(name), kind="all")
        return None

    def __init__(self, transfer_root: Path | None = None, gateway_root: str = "") -> None:
        self._root = transfer_root.absolute() if transfer_root is not None else None
        self._gateway_root = gateway_root

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        if message.media is not None and message.media.kind == "file":
            if message.text or message.mentions:
                return (
                    replace(message, text="", mentions=(), reply_reference=None),
                    replace(message, media=None),
                )
            return (replace(message, reply_reference=None),)
        return (message,)

    @asynccontextmanager
    async def _file(self, content: bytes) -> AsyncIterator[str]:
        if self._root is None or not PurePosixPath(self._gateway_root).is_absolute():
            raise TransportEffectError("artifact_transport_unavailable", uncertain=False)
        root = self._root
        if root.is_symlink() or root.resolve() != root:
            raise TransportEffectError("unsafe_transfer_root", uncertain=False)
        token = str(uuid4())
        path = root / token
        root.mkdir(parents=True, exist_ok=True)
        worker = asyncio.create_task(asyncio.to_thread(_write_handoff, path, content))
        cancelled = None
        try:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            if cancelled is not None:
                raise cancelled
            try:
                worker.result()
            except OSError as exc:
                raise TransportEffectError(type(exc).__name__, uncertain=False) from exc
            yield str(PurePosixPath(self._gateway_root) / token)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).exception("social_transfer_cleanup_failed")

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt:
        bot = cast(Any, handle)
        group = target.kind == "space"
        destination = {"group_id" if group else "user_id": int(target.external_id)}
        media = message.media
        if media is not None and media.kind == "file":
            async with self._file(media.content) as path:
                result = await bot.call_api(
                    "upload_group_file" if group else "upload_private_file",
                    **destination,
                    file=path,
                    name=media.filename,
                )
            reference = None
            if isinstance(result, dict):
                for key, prefix in (("message_id", ""), ("file_id", "file:")):
                    value = result.get(key)
                    if (
                        isinstance(value, str | int)
                        and not isinstance(value, bool)
                        and str(value).strip()
                    ):
                        reference = prefix + str(value).strip()
                        break
            return TransportReceipt(reference)
        parts: list[dict[str, Any]] = []
        if message.reply_reference is not None:
            parts.append({"type": "reply", "data": {"id": message.reply_reference}})
        cursor = 0
        for mention in message.mentions:
            if mention.text_start > cursor:
                parts.append(
                    {"type": "text", "data": {"text": message.text[cursor : mention.text_start]}}
                )
            parts.append(
                {
                    "type": "at",
                    "data": {"qq": "all" if mention.kind == "all" else mention.external_id},
                }
            )
            cursor = mention.text_end
        if cursor < len(message.text):
            parts.append({"type": "text", "data": {"text": message.text[cursor:]}})
        if media is not None:
            parts.append(
                {
                    "type": "image",
                    "data": {
                        "file": "base64://" + base64.b64encode(media.content).decode("ascii"),
                        **({"sub_type": 1} if media.emoji else {}),
                    },
                }
            )
        result = await bot.call_api(
            "send_group_msg" if group else "send_private_msg", **destination, message=parts
        )
        return TransportReceipt(parse_onebot_send_receipt(result).platform_message_id)

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None:
        await cast(Any, handle).call_api("delete_msg", message_id=reference)

    async def is_member(self, handle: object, space: str, account: str) -> bool:
        result = await cast(Any, handle).call_api(
            "get_group_member_info", group_id=int(space), user_id=int(account), no_cache=True
        )
        return isinstance(result, dict) and str(result.get("user_id")) == account
