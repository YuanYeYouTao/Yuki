"""Pairing URL configuration preserves exact codes and actual deployment prefixes."""

from urllib.parse import parse_qs, urlsplit

import pytest
from tests.conftest import make_settings

from yuki.client_api.links import pairing_reply, public_base_url


@pytest.mark.parametrize(
    "server",
    [
        "",
        "https://example.com",
        "https://EXAMPLE.com/Role/",
        "http://127.0.0.1:8080/role",
        "https://example.com/%E8%A7%92%E8%89%B2",
    ],
)
def test_pairing_url_preserves_the_whole_code_and_configured_prefix(server):
    code = "aB_-0123456789012345678901234567"
    assert len(code) == 32
    response = pairing_reply(code, server)
    assert f"绑定码：{code}" in response
    assert "10 分钟" in response and "使用一次" in response
    if not server:
        assert "yuki://" not in response
    else:
        link = response.split("绑定链接：", 1)[1]
        assert parse_qs(urlsplit(link).query) == {"server": [server.rstrip("/")], "code": [code]}
    settings = make_settings("sqlite+aiosqlite:///:memory:", client_public_url=server)
    assert settings.client_public_url == public_base_url(server)


@pytest.mark.parametrize(
    "server",
    [
        "/",
        "example.com/role",
        "ssh://example.com",
        "https://user:secret@example.com",
        "https://example.com?next=/other",
        "https://example.com/#fragment",
        "https://example.com/has space",
        "https://example.com:99999",
        "https://example.com\\other",
        "https://example.com/\x7f",
    ],
)
def test_public_pairing_base_rejects_ambiguous_or_credentialed_urls(server):
    with pytest.raises(ValueError):
        public_base_url(server)
    with pytest.raises(ValueError):
        make_settings("sqlite+aiosqlite:///:memory:", client_public_url=server)
