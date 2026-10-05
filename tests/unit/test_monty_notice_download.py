from __future__ import annotations

import io
import urllib.error

import pytest
from scripts import export_monty_notices as notices


def test_pinned_notice_read_retries_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[str, int]] = []
    pauses: list[int] = []

    def read(url: str, *, timeout: int) -> io.BytesIO:
        attempts.append((url, timeout))
        if len(attempts) < 3:
            raise urllib.error.URLError("TLS connection interrupted")
        return io.BytesIO(b"original license text")

    monkeypatch.setattr(notices.urllib.request, "urlopen", read)
    monkeypatch.setattr(notices.time, "sleep", pauses.append)
    url = "https://raw.githubusercontent.com/example/project/" + "a" * 40 + "/LICENSE"
    assert notices.read_upstream(url) == b"original license text"
    assert attempts == [(url, 30)] * 3
    assert pauses == [1, 2]


@pytest.mark.parametrize("status", [403, 404, 503])
def test_notice_absence_is_distinct_from_failed_audit(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    attempts = 0

    def read(url: str, *, timeout: int) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(url, status, "fixture", {}, None)

    monkeypatch.setattr(notices.urllib.request, "urlopen", read)
    monkeypatch.setattr(notices.time, "sleep", lambda _: None)
    if status == 404:
        assert notices.read_upstream("https://example.invalid/LICENSE") is None
    else:
        with pytest.raises(urllib.error.HTTPError) as failure:
            notices.read_upstream("https://example.invalid/LICENSE")
        assert failure.value.code == status
    assert attempts == (3 if status == 503 else 1)
