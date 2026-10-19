"""Configured public pairing links; credentials and request headers are never URL sources."""

from urllib.parse import urlencode, urlsplit


def public_base_url(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in normalized
        or "#" in normalized
        or "\\" in normalized
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise ValueError(
            "CLIENT_PUBLIC_URL must be an HTTP(S) base URL without credentials, query or fragment"
        )
    _ = parsed.port  # Validate the actual port, including its numeric range.
    return normalized.rstrip("/")


def pairing_reply(code: str, server: str = "") -> str:
    response = f"绑定码：{code}（10 分钟内有效，使用一次后失效）。"
    base = public_base_url(server)
    if base:
        response += "\n绑定链接：yuki://pair?" + urlencode({"server": base, "code": code})
    return response
