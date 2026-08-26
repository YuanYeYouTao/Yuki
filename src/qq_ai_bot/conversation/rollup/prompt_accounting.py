"""Prompt-visible, durable-watermark, and compression-source character accounting.

Foreground fit and protected-tail Prompt characters use grouped main-agent
history. Stored ``uncovered_character_count`` uses an additive per-message
durable ruler that does not group senders or look up neighbors. Compression
batches cut on the exact serialized New source events string.
"""

from __future__ import annotations

from collections.abc import Iterable

from qq_ai_bot.conversation.rollup.renderer import (
    bound_compaction_source_events,
    rollup_source_projection,
    serialize_compaction_source_events,
)
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord

DEFAULT_BOT_DISPLAY_NAME = "Yuki"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def is_prompt_visible_message(
    event: EventRecord,
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> bool:
    """Return whether one keeper occupies a main-history speaker slot.

    Visibility matches the isolated production renderer: ``event_kind=message``
    and a nonblank ``render_reference_event`` projection. Blank and silently
    dropped rows do not occupy protected-tail slots. Mention-only and other
    segment-visible messages remain visible.
    """

    if event.event_kind != "message":
        return False
    renderer = ChatEventPromptRenderer(
        (event,),
        bot_display_name=bot_display_name,
        timezone=timezone,
    )
    return bool(renderer.render_reference_event(event).strip())


def prompt_visible_events(
    events: Iterable[EventRecord],
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> tuple[EventRecord, ...]:
    """Return keepers that project into ordinary main-agent history."""

    return tuple(
        event
        for event in events
        if is_prompt_visible_message(
            event,
            bot_display_name=bot_display_name,
            timezone=timezone,
        )
    )


def prompt_visible_event_count(
    events: Iterable[EventRecord],
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return the prompt-visible message count for one keeper sequence."""

    return sum(
        1
        for event in events
        if is_prompt_visible_message(
            event,
            bot_display_name=bot_display_name,
            timezone=timezone,
        )
    )


def prompt_accounting_characters(
    events: Iterable[EventRecord],
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return grouped main-agent history characters for foreground fit.

    External source rows contribute zero. Adjacent same-sender messages may
    share an envelope. This is the actual Prompt window ruler, not the stored
    durable watermark.
    """

    rows = tuple(events)
    renderer = ChatEventPromptRenderer(
        rows,
        bot_display_name=bot_display_name,
        timezone=timezone,
    )
    rendered = renderer.main_agent_history(rows)
    return sum(len(item.content or "") for _, _, item in rendered)


def durable_uncovered_event_characters(
    event: EventRecord,
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return the additive durable watermark increment for one keeper.

    External events are zero. The projection is isolated to this event: no
    adjacent-sender grouping and no neighbor lookup.
    """

    if not is_prompt_visible_message(
        event,
        bot_display_name=bot_display_name,
        timezone=timezone,
    ):
        return 0
    renderer = ChatEventPromptRenderer(
        (event,),
        bot_display_name=bot_display_name,
        timezone=timezone,
    )
    return len(renderer.render_reference_event(event))


def durable_uncovered_characters(
    events: Iterable[EventRecord],
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return the stored uncovered-character watermark for one keeper sequence."""

    return sum(
        durable_uncovered_event_characters(
            event,
            bot_display_name=bot_display_name,
            timezone=timezone,
        )
        for event in events
    )


def prompt_accounting_event_characters(
    event: EventRecord,
    *,
    events: Iterable[EventRecord] = (),
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return the durable character increment for one appended keeper.

    ``events`` is ignored so neighbors cannot change the stored watermark.
    External source rows are diagnostic keepers only: the increment is zero and
    must not force-wake an existing rollup job.
    """

    del events
    return durable_uncovered_event_characters(
        event,
        bot_display_name=bot_display_name,
        timezone=timezone,
    )


def source_accounting_event_characters(
    event: EventRecord,
    *,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return unbounded compression-source characters for one keeper."""

    return len(rollup_source_projection(event, timezone=timezone))


def source_accounting_characters(
    events: Iterable[EventRecord],
    *,
    timezone: str = DEFAULT_TIMEZONE,
    max_characters: int | None = None,
) -> int:
    """Return serialized compaction-source characters for one batch.

    When ``max_characters`` is set, the cost matches the bounded New source
    events string sent to the model, including separators.
    """

    rows = tuple(events)
    if max_characters is None:
        return len(serialize_compaction_source_events(rows, timezone=timezone))
    return len(
        bound_compaction_source_events(
            rows,
            timezone=timezone,
            max_characters=max_characters,
        )
    )
