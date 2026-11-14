"""QQ official Bot deliveries: passive replies by platform fact, never by prediction.

Every delivery carries a passive ``msg_id`` when one exists: the trigger event's own,
or for a group the newest inbound message. Window and count limits are not predicted;
the platform's refusal codes are the receipts. Groups have no proactive messages, and
nothing is redirected to a private chat or another platform.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any, cast
from urllib.parse import quote

from yuki.domain.conversations import ConversationScope
from yuki.gateway.providers.qq_official import (
    QQOfficialAPIError,
    QQOfficialClient,
    QQOfficialConnection,
    QQOfficialNoDeliveryPath,
)
from yuki.gateway.providers.qq_official_store import (
    latest_inbound,
    next_msg_seq,
    record_message,
    ref_idx_for,
    trigger_message,
)
from yuki.gateway.registry import RegistryClosed
from yuki.gateway.social import (
    ForwardNode,
    TransportMedia,
    TransportMention,
    TransportMessage,
    TransportReceipt,
    TransportTarget,
)

# 40054005: deduplicated by msg_seq; one more attempt with the next msg_seq passes.
_DEDUPLICATED = 40054005
# 40034122: this period's wakeup is used; 40034123: wakeup unsupported for this user.
_WAKEUP_REFUSED = frozenset({40034122, 40034123})
# Upload hashes cover the first 10002432 bytes for md5_10m (platform convention).
_MD5_HEAD = 10002432
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def _chat(target: TransportTarget) -> tuple[str, str]:
    if target.kind == "space":
        return "group", "/v2/groups/" + quote(target.external_id, safe="")
    return "c2c", "/v2/users/" + quote(target.external_id, safe="")


def _markdown(text: str) -> str:
    """QQ Markdown folds single newlines; separate lines unless one block continues."""
    lines: list[str] = []
    fenced = False
    previous = ""
    for line in text.split("\n"):
        continues = (
            fenced
            or not previous
            or not line.strip()
            or (_LIST_ITEM.match(previous) and _LIST_ITEM.match(line))
            or (previous.lstrip().startswith(">") and line.lstrip().startswith(">"))
        )
        if not continues:
            lines.append("")
        lines.append(line)
        if line.strip().startswith("```"):
            fenced = not fenced
        previous = line.strip() and line
    return "\n".join(lines)


def _with_mentions(text: str, mentions: tuple[TransportMention, ...]) -> str:
    pieces: list[str] = []
    cursor = 0
    for mention in mentions:
        if mention.kind == "all":
            raise ValueError("all_mentions_unavailable")
        pieces.extend(
            (text[cursor : mention.text_start], f'<qqbot-at-user id="{mention.external_id}" />')
        )
        cursor = mention.text_end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _digest(name: str, data: bytes) -> str:
    return hashlib.new(name, data, usedforsecurity=False).hexdigest()


async def _upload(client: QQOfficialClient, prefix: str, media: TransportMedia) -> str:
    """Chunked upload; a failure here means the message itself was never sent."""
    data = media.content
    file_type = 1 if media.kind == "image" else 4
    try:
        prepared = await client.call(
            "POST",
            prefix + "/upload_prepare",
            {
                "file_type": file_type,
                "file_size": str(len(data)),
                "file_name": media.filename,
                "md5": _digest("md5", data),
                "sha1": _digest("sha1", data),
                "md5_10m": _digest("md5", data[:_MD5_HEAD]),
            },
        )
        offset = 0
        for part in sorted(prepared["parts"], key=lambda item: int(item["index"])):
            size = int(part.get("block_size") or prepared["block_size"])
            chunk = data[offset : offset + size]
            offset += len(chunk)
            await client.put_part(part["presigned_url"], chunk)
            await client.call(
                "POST",
                prefix + "/upload_part_finish",
                {
                    "upload_id": prepared["upload_id"],
                    "part_index": part["index"],
                    "block_size": str(len(chunk)),
                    "md5": _digest("md5", chunk),
                },
            )
        merged = await client.call(
            "POST",
            prefix + "/files",
            {
                "file_type": file_type,
                "file_name": media.filename,
                "upload_id": prepared["upload_id"],
                "srv_send_msg": False,
            },
        )
    except QQOfficialAPIError as exc:
        raise QQOfficialAPIError(
            "qq_official_upload_failed", status_code=exc.status_code, error_code=exc.error_code
        ) from None
    return str(merged["file_info"])


async def _body(
    connection: QQOfficialConnection, chat_kind: str, prefix: str, message: TransportMessage
) -> dict[str, Any]:
    if message.media is not None:
        file_info = await _upload(connection.client, prefix, message.media)
        return {"msg_type": 7, "media": {"file_info": file_info}}
    body: dict[str, Any]
    if chat_kind == "group" and message.mentions:
        # Group @ exists only as a tag inside a Markdown message.
        content = _markdown(_with_mentions(message.text, message.mentions))
        body = {"msg_type": 2, "markdown": {"content": content}}
    else:
        body = {"msg_type": 0, "content": message.text}
    if message.reply_reference is not None:
        # Quotes address REFIDX_ keys. An unknown one is left out rather than letting
        # the platform refuse the whole message.
        ref_idx = await ref_idx_for(
            connection.database, connection.presence_id, message.reply_reference
        )
        if ref_idx is not None:
            body["message_reference"] = {"message_id": ref_idx}
    return body


async def send_message(
    connection: QQOfficialConnection,
    target: TransportTarget,
    message: TransportMessage,
    *,
    reply_to: str | None,
) -> TransportReceipt:
    """One delivery; ``reply_to`` is the trigger's platform msg_id when it has one."""
    chat_kind, prefix = _chat(target)
    database, presence_id = connection.database, connection.presence_id
    if reply_to is None and chat_kind == "group":
        reply_to = await latest_inbound(database, presence_id, chat_kind, target.external_id)
        if reply_to is None:
            raise QQOfficialNoDeliveryPath("qq_official_no_delivery_path")
    body = await _body(connection, chat_kind, prefix, message)
    client, path = connection.client, prefix + "/messages"
    seq = None
    if reply_to is not None:
        # The next seq follows the durable delivery records of this msg_id.
        seq = await next_msg_seq(database, presence_id, reply_to)
        try:
            result = await client.call("POST", path, {**body, "msg_id": reply_to, "msg_seq": seq})
        except QQOfficialAPIError as exc:
            if exc.error_code != _DEDUPLICATED:
                raise
            seq += 1
            result = await client.call("POST", path, {**body, "msg_id": reply_to, "msg_seq": seq})
    else:
        try:
            result = await client.call("POST", path, {**body, "is_wakeup": True})
        except QQOfficialAPIError as exc:
            if exc.error_code not in _WAKEUP_REFUSED:
                raise
            result = await client.call("POST", path, body)
    message_id = result.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise QQOfficialAPIError("qq_official_receipt_missing", uncertain=True)
    ext_info = result.get("ext_info")
    await record_message(
        database,
        presence_id=presence_id,
        message_id=message_id,
        chat_kind=chat_kind,
        chat_id=target.external_id,
        direction="outbound",
        ref_idx=ext_info.get("ref_idx") if isinstance(ext_info, dict) else None,
        reply_to=reply_to,
        msg_seq=seq,
    )
    return TransportReceipt(message_id)


class QQOfficialSocialOperations:
    def native_reference_namespace(
        self, *, account_id: str, scope: ConversationScope
    ) -> tuple[str | None, ...]:
        return (
            scope.platform,
            scope.scope_type.value,
            scope.group_id or scope.private_peer_user_id,
            scope.thread_id,
        )

    def native_mention(self, text: str, offset: int) -> TransportMention | None:
        # Members are addressed only by openid; there is no textual address form.
        return None

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        if message.media is not None and (message.text or message.mentions):
            # Rich media has no caption: text and file are two receipted deliveries.
            return (
                replace(message, media=None),
                replace(message, text="", mentions=(), reply_reference=None),
            )
        return (message,)

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt:
        connection = cast(QQOfficialConnection, handle)
        reply_to = (
            await trigger_message(
                connection.database, connection.presence_id, target.source_event_id
            )
            if target.source_event_id is not None
            else None
        )
        return await send_message(connection, target, message, reply_to=reply_to)

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None:
        _kind, prefix = _chat(target)
        await cast(QQOfficialConnection, handle).client.call(
            "DELETE", f"{prefix}/messages/{quote(reference, safe='')}"
        )

    async def is_member(self, handle: object, space: str, account: str) -> bool:
        """Only the Bot itself is probed; the platform pushes a group's events only to
        Bots in that group, so a received group message is the membership evidence."""
        connection = cast(QQOfficialConnection, handle)
        return (
            account == connection.client.self_id
            and await latest_inbound(connection.database, connection.presence_id, "group", space)
            is not None
        )

    async def read_forward(self, handle: object, locator: str) -> tuple[ForwardNode, ...]:
        raise RegistryClosed("capability")
