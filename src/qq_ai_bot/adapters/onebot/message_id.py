"""OneBot message IDs are signed ASCII decimal integers."""

import re


def parse_message_id(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"-?[0-9]+", value) is None:
        raise ValueError("invalid OneBot message ID")
    return int(value)
