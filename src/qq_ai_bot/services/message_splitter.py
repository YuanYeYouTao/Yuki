"""Pure QQ message splitting for explicit outbound delivery."""

from __future__ import annotations

import re

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.services.renderer import split_qq_message

_FENCED_BLOCK = re.compile(r"(```[^\n]*\n.*?\n```|~~~[^\n]*\n.*?\n~~~)", re.DOTALL)
_CHAT_LINE_BREAK = re.compile(r"\n+")
_STRUCTURED_CHAT_OUTPUT = re.compile(
    r"(?m)^[ \t]*(?:```|~~~|[-*+]\s+|\d+[.)、]\s+|>\s+|#{1,6}\s+|\|.*\|[ \t]*$)"
)


class OutboundMessageSplitter:
    """Split text deterministically; transport owns effects and receipts."""

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
