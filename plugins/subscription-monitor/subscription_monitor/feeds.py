"""Bounded, network-free projection of RSS 2, Atom and JSON Feed entries.

Duplicate IDs keep the first entry in document order, including conflicting
duplicates. IDs are source identities, not execution identities or authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Annotated
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element, ParseError, tostring

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import Field

from yuki_plugin_sdk.models import StrictModel

MAX_FEED_BYTES = 1_048_576
MAX_FEED_ENTRIES = 200
MAX_CONTENT_CHARS = 6000
ATOM = "{http://www.w3.org/2005/Atom}"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


class FeedEntry(StrictModel):
    id: str = Field(min_length=1, max_length=1024)
    title: str = Field(max_length=500)
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    url: str = Field(max_length=4096)
    author: str = Field(default="", max_length=300)
    tags: tuple[Annotated[str, Field(max_length=100)], ...] = Field(default=(), max_length=32)
    published_at: datetime | None = None


class FeedParseError(ValueError):
    """The response cannot safely be treated as a complete supported feed."""


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        if not self.hidden_depth and tag in {
            "p",
            "div",
            "br",
            "li",
            "ul",
            "ol",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "tr",
            "td",
        }:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        if not self.hidden_depth and tag in {"p", "div", "li", "blockquote", "tr", "td"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _plain(value: str) -> str:
    return " ".join(value.split())


def _html(value: str) -> str:
    parser = _HTMLText()
    parser.feed(value)
    parser.close()
    return _plain("".join(parser.parts))


def _url(value: str, base: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        return ""
    try:
        resolved = urljoin(base, value)
        parts = urlsplit(resolved)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or len(resolved) > 4096
        ):
            return ""
        # Accessing port also rejects malformed/out-of-range ports.
        _ = parts.port
    except ValueError:
        return ""
    return resolved


def _date(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    try:
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _entry(
    *,
    source_url: str,
    identity: str,
    title: str,
    content: str,
    url: str,
    author: str = "",
    tags: tuple[str, ...] = (),
    date: str = "",
) -> FeedEntry:
    title, content, author = _plain(title), _plain(content), _plain(author)
    identity = identity.strip() or url
    if not identity:
        # Hash before truncation so entries differing beyond display bounds stay distinct.
        identity = (
            "sha256:"
            + hashlib.sha256(
                json.dumps([source_url, title, content, author], ensure_ascii=False).encode()
            ).hexdigest()
        )
    elif len(identity) > 1024:
        identity = "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
    return FeedEntry(
        id=identity,
        title=title[:500],
        content=content[:MAX_CONTENT_CHARS],
        url=url,
        author=author[:300],
        tags=tuple(dict.fromkeys(_plain(tag)[:100] for tag in tags if _plain(tag)))[:32],
        published_at=_date(date),
    )


def _text(parent: Element, tag: str) -> str:
    child = parent.find(tag)
    return "" if child is None else "".join(child.itertext())


def _xml_content(element: Element | None, *, default_type: str = "html") -> str:
    if element is None:
        return ""
    if len(element):
        raw = (element.text or "") + "".join(
            tostring(child, encoding="unicode") for child in element
        )
        # Namespace prefixes are irrelevant to text extraction.
        return _html(raw)
    raw = element.text or ""
    return _plain(raw) if element.get("type", default_type) == "text" else _html(raw)


def _xml_base(element: Element, parent_base: str) -> str:
    return _url(element.get(XML_BASE, ""), parent_base) or parent_base


def _rss(root: Element, source_url: str) -> list[FeedEntry]:
    channel = root.find("channel")
    if channel is None:
        raise FeedParseError("RSS feed has no channel")
    items = channel.findall("item")
    _check_count(len(items))
    base = _xml_base(channel, _xml_base(root, source_url))
    result: list[FeedEntry] = []
    for item in items:
        item_base = _xml_base(item, base)
        guid = item.find("guid")
        identity = _text(item, "guid")
        link = _url(_text(item, "link"), item_base)
        if not link and guid is not None and guid.get("isPermaLink", "true").lower() == "true":
            link = _url(identity, item_base)
        content = item.find(CONTENT)
        if content is None:
            content = item.find("description")
        result.append(
            _entry(
                source_url=source_url,
                identity=identity,
                title=_html(_text(item, "title")),
                content=_xml_content(content),
                url=link,
                author=_html(_text(item, "author") or _text(item, CREATOR)),
                tags=tuple("".join(tag.itertext()) for tag in item.findall("category")),
                date=_text(item, "pubDate")
                or _text(item, "{http://purl.org/dc/elements/1.1/}date"),
            )
        )
    return result


def _atom(root: Element, source_url: str) -> list[FeedEntry]:
    items = root.findall(f"{ATOM}entry")
    _check_count(len(items))
    base = _xml_base(root, source_url)
    result: list[FeedEntry] = []
    for item in items:
        item_base = _xml_base(item, base)
        links = [
            link
            for link in item.findall(f"{ATOM}link")
            if link.get("rel", "alternate") == "alternate"
        ]
        links.sort(key=lambda link: link.get("type", "text/html") != "text/html")
        link_url = next(
            (
                url
                for link in links
                if (url := _url(link.get("href", ""), _xml_base(link, item_base)))
            ),
            "",
        )
        content = item.find(f"{ATOM}content")
        if content is None:
            content = item.find(f"{ATOM}summary")
        author = _text(item, f"{ATOM}author/{ATOM}name") or _text(root, f"{ATOM}author/{ATOM}name")
        result.append(
            _entry(
                source_url=source_url,
                identity=_text(item, f"{ATOM}id"),
                title=_xml_content(item.find(f"{ATOM}title"), default_type="text"),
                content=_xml_content(content, default_type="text"),
                url=link_url,
                author=author,
                tags=tuple(tag.get("term", "") for tag in item.findall(f"{ATOM}category")),
                date=_text(item, f"{ATOM}published") or _text(item, f"{ATOM}updated"),
            )
        )
    return result


def _string(data: dict[str, object], key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise FeedParseError(f"JSON Feed {key} must be a string")
    return value


def _json_author(data: dict[str, object]) -> str:
    authors = data.get("authors")
    if authors is None:
        author = data.get("author")
        authors = [] if author is None else [author]
    if not isinstance(authors, list) or any(not isinstance(author, dict) for author in authors):
        raise FeedParseError("JSON Feed authors must contain objects")
    return ", ".join(_string(author, "name") for author in authors)


def _json_feed(body: str, source_url: str) -> list[FeedEntry]:
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("version") not in {
        "https://jsonfeed.org/version/1",
        "https://jsonfeed.org/version/1.1",
    }:
        raise FeedParseError("Expected JSON Feed version 1 or 1.1")
    items = data.get("items")
    if not isinstance(items, list):
        raise FeedParseError("JSON Feed items must be an array")
    _check_count(len(items))
    result: list[FeedEntry] = []
    for item in items:
        if not isinstance(item, dict):
            raise FeedParseError("JSON Feed items must contain objects")
        if "content_text" not in item and "content_html" not in item:
            raise FeedParseError("JSON Feed item has no content_text or content_html")
        content = (
            _string(item, "content_text")
            if "content_text" in item
            else _html(_string(item, "content_html"))
        )
        tags = item.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise FeedParseError("JSON Feed tags must contain strings")
        result.append(
            _entry(
                source_url=source_url,
                identity=_string(item, "id"),
                title=_string(item, "title"),
                content=content,
                url=_url(_string(item, "url"), source_url),
                author=_json_author(item)
                if "authors" in item or "author" in item
                else _json_author(data),
                tags=tuple(tags),
                date=_string(item, "date_published") or _string(item, "date_modified"),
            )
        )
    return result


def _check_count(count: int) -> None:
    if count > MAX_FEED_ENTRIES:
        raise FeedParseError(f"Feed exceeds {MAX_FEED_ENTRIES} entries")


def parse_feed(body: str, *, source_url: str) -> tuple[FeedEntry, ...]:
    """Parse a complete response; oversize/invalid responses fail before advancing state."""
    try:
        oversized = len(body) > MAX_FEED_BYTES or len(body.encode("utf-8")) > MAX_FEED_BYTES
    except UnicodeError as exc:
        raise FeedParseError("Feed contains invalid Unicode") from exc
    if oversized:
        raise FeedParseError(f"Feed exceeds {MAX_FEED_BYTES} bytes")
    body = body.lstrip("\ufeff \t\r\n")
    source_url = _url(source_url, "")
    if not source_url:
        raise FeedParseError("Feed source_url must be an HTTP(S) URL")
    try:
        if body.startswith("{") or body.startswith("["):
            entries = _json_feed(body, source_url)
        else:
            root = ElementTree.fromstring(
                body, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
            if root.tag == "rss" and root.get("version", "2.0") == "2.0":
                entries = _rss(root, source_url)
            elif root.tag == f"{ATOM}feed":
                entries = _atom(root, source_url)
            else:
                raise FeedParseError("Expected RSS 2, Atom, or JSON Feed")
    except (
        ParseError,
        DefusedXmlException,
        json.JSONDecodeError,
        RecursionError,
        UnicodeError,
    ) as exc:
        raise FeedParseError("Feed is malformed or unsafe") from exc
    unique: dict[str, FeedEntry] = {}
    for entry in entries:
        unique.setdefault(entry.id, entry)
    return tuple(unique.values())
