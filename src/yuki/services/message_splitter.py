"""Pure QQ message splitting for explicit outbound delivery."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.gateway.social import TransportMention, TransportMessage, split_transport_message
from yuki.services.renderer import split_qq_message

_FENCED_BLOCK = re.compile(r"(```[^\n]*\n.*?\n```|~~~[^\n]*\n.*?\n~~~)", re.DOTALL)
_CHAT_LINE_BREAK = re.compile(r"\n+")
_STRUCTURED_CHAT_OUTPUT = re.compile(
    r"(?m)^[ \t]*(?:```|~~~|[-*+]\s+|\d+[.)、]\s+|>\s+|#{1,6}\s+|\|.*\|[ \t]*$)"
)


def _join_messages(parts: Iterable[TransportMessage]) -> TransportMessage:
    text: list[str] = []
    mentions: list[TransportMention] = []
    length = 0
    for part in parts:
        if text:
            text.append("\n")
            length += 1
        text.append(part.text)
        mentions.extend(
            replace(item, text_start=item.text_start + length, text_end=item.text_end + length)
            for item in part.mentions
        )
        length += len(part.text)
    return TransportMessage("".join(text), tuple(mentions))


def _trim_range(text: str, start: int, end: int) -> tuple[int, int]:
    raw = text[start:end]
    return start + len(raw) - len(raw.lstrip()), end - len(raw) + len(raw.rstrip())


def _plain_ranges(text: str, limit: int) -> list[tuple[int, int]]:
    """The paragraph/sentence policy as source intervals, before any rendering."""
    paragraphs = []
    start = 0
    for match in re.finditer(r"\n\s*\n", text):
        if start < match.start():
            paragraphs.append((start, match.start()))
        paragraphs.append((match.start(), match.end()))
        start = match.end()
    if start < len(text):
        paragraphs.append((start, len(text)))
    ranges = []
    current_start = current_end = 0
    for start, end in paragraphs:
        if end - start > limit:
            if current_start < current_end:
                ranges.append(_trim_range(text, current_start, current_end))
            current_start = current_end = end
            sentence_start = start
            sentence_current_start = sentence_current_end = start
            endpoints = (
                match.end() for match in re.finditer(r"(?<=[。！？!?；;])", text[start:end])
            )
            for sentence_end in (*[start + value for value in endpoints], end):
                if sentence_end <= sentence_start:
                    continue
                size = sentence_end - sentence_start
                if size > limit:
                    if sentence_current_start < sentence_current_end:
                        ranges.append(
                            _trim_range(text, sentence_current_start, sentence_current_end)
                        )
                    ranges.extend(
                        _trim_range(text, index, min(index + limit, sentence_end))
                        for index in range(sentence_start, sentence_end, limit)
                    )
                    sentence_current_start = sentence_current_end = sentence_end
                elif sentence_end - sentence_current_start <= limit:
                    sentence_current_end = sentence_end
                else:
                    ranges.append(_trim_range(text, sentence_current_start, sentence_current_end))
                    sentence_current_start, sentence_current_end = sentence_start, sentence_end
                sentence_start = sentence_end
            if sentence_current_start < sentence_current_end:
                ranges.append(_trim_range(text, sentence_current_start, sentence_current_end))
        elif end - current_start <= limit:
            current_end = end
        else:
            if current_start < current_end:
                ranges.append(_trim_range(text, current_start, current_end))
            current_start, current_end = start, end
    if current_start < current_end:
        ranges.append(_trim_range(text, current_start, current_end))
    return [(start, end) for start, end in ranges if start < end]


def _source_sections(
    message: TransportMessage, ranges: Iterable[tuple[int, int]]
) -> list[TransportMessage]:
    """Walk ordered source spans once; never recover offsets from rendered output."""
    atomic = []
    pending_start = None
    mention_index = 0
    for start, end in ranges:
        start = start if pending_start is None else pending_start
        while (
            mention_index < len(message.mentions)
            and message.mentions[mention_index].text_end <= end
        ):
            mention_index += 1
        if (
            mention_index < len(message.mentions)
            and message.mentions[mention_index].text_start < end
        ):
            pending_start = start
            continue
        atomic.append((start, end))
        pending_start = None
    sections = []
    mention_index = 0
    for start, end in atomic:
        selected = []
        while (
            mention_index < len(message.mentions)
            and message.mentions[mention_index].text_start < end
        ):
            item = message.mentions[mention_index]
            selected.append(
                replace(item, text_start=item.text_start - start, text_end=item.text_end - start)
            )
            mention_index += 1
        sections.append(
            replace(
                message,
                text=message.text[start:end],
                mentions=tuple(selected),
                reply_reference=None,
            )
        )
    return sections


def _split_spanned_structure(message: TransportMessage, limit: int) -> tuple[TransportMessage, ...]:
    if len(message.text) <= limit:
        return (message,)
    ranges = []
    cursor = 0
    for matched_fence in _FENCED_BLOCK.finditer(message.text):
        if cursor < matched_fence.start():
            ranges.append((cursor, matched_fence.start()))
        ranges.append((matched_fence.start(), matched_fence.end()))
        cursor = matched_fence.end()
    if cursor < len(message.text):
        ranges.append((cursor, len(message.text)))
    chunks = []
    for section in _source_sections(message, ranges):
        fence = _FENCED_BLOCK.fullmatch(section.text)
        if fence is not None:
            opening_end, closing_start = section.text.index("\n") + 1, section.text.rindex("\n")
            if all(
                opening_end <= item.text_start and item.text_end <= closing_start
                for item in section.mentions
            ):
                opening, closing = section.text[:opening_end], section.text[closing_start:]
                (body,) = _source_sections(section, ((opening_end, closing_start),))
                available = max(1, limit - len(opening) - len(closing))
                lines = []
                start = 0
                for line in body.text.splitlines(keepends=True):
                    lines.append((start, start + len(line.rstrip("\r\n"))))
                    start += len(line)
                groups: list[tuple[int, int]] = []
                for start, end in lines:
                    if groups and end - groups[-1][0] <= available:
                        groups[-1] = (groups[-1][0], end)
                    else:
                        groups.append((start, end))
                for part in _source_sections(body, groups):
                    for bounded in split_transport_message(part, available):
                        chunks.append(
                            replace(
                                bounded,
                                text=opening + bounded.text + closing,
                                mentions=tuple(
                                    replace(
                                        item,
                                        text_start=item.text_start + len(opening),
                                        text_end=item.text_end + len(opening),
                                    )
                                    for item in bounded.mentions
                                ),
                            )
                        )
                continue
        for part in _source_sections(section, _plain_ranges(section.text, limit)):
            chunks.extend(split_transport_message(part, limit))
    return tuple(chunks)


class OutboundMessageSplitter:
    """Split text deterministically; transport owns effects and receipts."""

    @staticmethod
    def render_message(
        message: TransportMessage, *, runtime: RuntimeConfigSnapshot
    ) -> tuple[TransportMessage, ...]:
        if not message.mentions:
            return tuple(
                replace(
                    message,
                    text=text,
                    reply_reference=message.reply_reference if index == 0 else None,
                )
                for index, text in enumerate(
                    OutboundMessageSplitter.render(message.text, runtime=runtime)
                )
            )
        hard_max = runtime.reply.hard_max_messages
        limit = runtime.reply.max_qq_message_chars
        structured = _STRUCTURED_CHAT_OUTPUT.search(message.text) is not None
        if structured:
            ranges = [(0, len(message.text))]
        else:
            ranges = []
            for match in re.finditer(r"[^\n]+", message.text):
                raw = match[0]
                start = match.start() + len(raw) - len(raw.lstrip())
                end = match.end() - (len(raw) - len(raw.rstrip()))
                if start < end:
                    ranges.append((start, end))
            if len(ranges) < 2:
                ranges = [(0, len(message.text))]
        sections = []
        mention_index = 0
        for start, end in ranges:
            selected = []
            while (
                mention_index < len(message.mentions)
                and message.mentions[mention_index].text_start < end
            ):
                item = message.mentions[mention_index]
                selected.append(
                    replace(
                        item, text_start=item.text_start - start, text_end=item.text_end - start
                    )
                )
                mention_index += 1
            sections.append(
                replace(
                    message,
                    text=message.text[start:end],
                    mentions=tuple(selected),
                    reply_reference=None,
                )
            )
        if not structured and len(sections) > hard_max:
            groups: list[list[TransportMessage]] = [[] for _ in range(hard_max)]
            for index, section in enumerate(sections):
                groups[min(index * hard_max // len(sections), hard_max - 1)].append(section)
            sections = [_join_messages(group) for group in groups if group]
        chunks = tuple(
            part for section in sections for part in _split_spanned_structure(section, limit)
        )
        if len(chunks) > hard_max:
            chunks = (*chunks[: hard_max - 1], _join_messages(chunks[hard_max - 1 :]))
        return tuple(
            replace(part, reply_reference=message.reply_reference if index == 0 else None)
            for index, part in enumerate(chunks)
        )

    @staticmethod
    def render(
        text: str,
        *,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[str, ...]:
        hard_max = runtime.reply.hard_max_messages
        structured = _STRUCTURED_CHAT_OUTPUT.search(text) is not None
        messages: tuple[str, ...]
        if structured:
            messages = (text,)
        else:
            messages = OutboundMessageSplitter._split_chat_line_sections(
                text, max_messages=hard_max
            )
        chunks = tuple(
            chunk
            for message in messages
            for chunk in OutboundMessageSplitter._split_preserving_structure(
                message, limit=runtime.reply.max_qq_message_chars
            )
        )
        if len(chunks) <= hard_max:
            return chunks
        return (*chunks[: hard_max - 1], "\n".join(chunks[hard_max - 1 :]))

    @staticmethod
    def _split_chat_line_sections(text: str, *, max_messages: int) -> tuple[str, ...]:
        sections = tuple(
            section.strip() for section in _CHAT_LINE_BREAK.split(text) if section.strip()
        )
        if len(sections) < 2:
            return (text,) if text else ()
        if len(sections) <= max_messages:
            return sections
        groups: list[list[str]] = [[] for _ in range(max_messages)]
        for index, section in enumerate(sections):
            group_index = min(index * max_messages // len(sections), max_messages - 1)
            groups[group_index].append(section)
        return tuple("\n".join(group) for group in groups if group)

    @staticmethod
    def _split_preserving_structure(text: str, *, limit: int) -> tuple[str, ...]:
        if len(text) <= limit:
            return (text,) if text else ()
        parts = _FENCED_BLOCK.split(text)
        chunks: list[str] = []
        for part in parts:
            if not part:
                continue
            if part.startswith(("```", "~~~")):
                chunks.extend(OutboundMessageSplitter._split_fenced_block(part, limit=limit))
            else:
                chunks.extend(split_qq_message(part, limit=limit))
        return tuple(chunk for chunk in chunks if chunk.strip())

    @staticmethod
    def _split_fenced_block(block: str, *, limit: int) -> tuple[str, ...]:
        lines = block.splitlines()
        if len(block) <= limit or len(lines) < 3:
            return (block,)
        opening = lines[0]
        closing = lines[-1]
        body = lines[1:-1]
        available = max(1, limit - len(opening) - len(closing) - 2)
        chunks: list[str] = []
        current: list[str] = []
        current_length = 0
        for line in body:
            extra = len(line) + int(bool(current))
            if current and current_length + extra > available:
                chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
                current = []
                current_length = 0
            if len(line) > available:
                if current:
                    chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
                    current = []
                    current_length = 0
                for index in range(0, len(line), available):
                    chunks.append(f"{opening}\n{line[index : index + available]}\n{closing}")
                continue
            current.append(line)
            current_length += extra
        if current:
            chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
        return tuple(chunks)
