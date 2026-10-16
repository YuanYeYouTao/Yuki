"""Hide credential-bearing HTTP locations in the caller's own async context."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_REDACT_HTTPX_URL: ContextVar[str | None] = ContextVar("http_redacted_url", default=None)


class _HttpxUrlRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        label = _REDACT_HTTPX_URL.get()
        if (
            label is not None
            and isinstance(record.msg, str)
            and record.msg.startswith("HTTP Request:")
            and isinstance(record.args, tuple)
            and len(record.args) >= 2
        ):
            arguments = list(record.args)
            arguments[1] = label
            record.args = tuple(arguments)
        return True


_HTTPX_URL_FILTER = _HttpxUrlRedactionFilter()


@contextmanager
def redact_httpx_url(label: str) -> Iterator[None]:
    logger = logging.getLogger("httpx")
    logger.disabled = False
    if _HTTPX_URL_FILTER not in logger.filters:
        logger.addFilter(_HTTPX_URL_FILTER)
    token = _REDACT_HTTPX_URL.set(label)
    try:
        yield
    finally:
        _REDACT_HTTPX_URL.reset(token)
