"""Current event source closure after the original envelope column rename."""

from yuki.conversation.projection_chunks_schema import (
    EVENT_METADATA_COLUMNS_CURRENT as _COLUMNS,
)
from yuki.conversation.projection_chunks_schema import (
    PROJECTION_TRIGGERS_CURRENT as _TRIGGERS,
)

# Native references do not change the frozen content/source fingerprint.
EVENT_METADATA_COLUMNS_CURRENT = tuple(
    "origin_key" if column == "platform_message_id" else column for column in _COLUMNS
)
PROJECTION_TRIGGERS_CURRENT = {
    name: sql.replace("platform_message_id", "origin_key") for name, sql in _TRIGGERS.items()
}
