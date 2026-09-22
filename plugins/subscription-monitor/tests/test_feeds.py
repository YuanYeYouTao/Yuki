from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from subscription_monitor.feeds import (
    MAX_CONTENT_CHARS,
    MAX_FEED_BYTES,
    FeedParseError,
    parse_feed,
)

SOURCE = "https://example.com/feeds/main.xml"


def test_rss_preserves_document_order_strips_html_and_normalizes_dates() -> None:
    entries = parse_feed(
        """<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel><item><guid isPermaLink="false">new</guid><title>News &amp; updates</title>
        <link>../posts/new</link><pubDate>Tue, 22 Sep 2026 14:00:00 +0800</pubDate>
        <description>short</description><content:encoded><![CDATA[<p>Hello <b>world</b></p>
        <script>secret()</script><style>body{}</style><p>Next &amp; last.</p>]]></content:encoded>
        <author>Editor</author><category>news</category><category>news</category>
      </item><item><guid>https://example.com/older</guid><title>Older</title></item></channel>
    </rss>""",
        source_url=SOURCE,
    )
    assert [entry.id for entry in entries] == ["new", "https://example.com/older"]
    assert entries[0].title == "News & updates"
    assert entries[0].content == "Hello world Next & last."
    assert entries[0].url == "https://example.com/posts/new"
    assert entries[0].published_at == datetime(2026, 9, 22, 6, tzinfo=UTC)
    assert entries[0].author == "Editor"
    assert entries[0].tags == ("news",)
    assert entries[1].url == "https://example.com/older"


def test_atom_uses_alternate_link_xml_base_and_feed_author() -> None:
    entries = parse_feed(
        """<feed xmlns="http://www.w3.org/2005/Atom" xml:base="../">
      <author><name>Team</name></author><entry xml:base="posts/">
        <id>tag:example.com,2026:post-1</id><title type="html">New &lt;b&gt;post&lt;/b&gt;</title>
        <link rel="self" href="https://api.example.com/1"/>
        <link rel="alternate" type="text/html" href="1"/>
        <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>First</p><p>Second</p></div></content>
        <updated>2026-09-22T10:01:02+08:00</updated><category term="release"/>
      </entry></feed>""",
        source_url=SOURCE,
    )
    (entry,) = entries
    assert entry.id == "tag:example.com,2026:post-1"
    assert entry.title == "New post"
    assert entry.content == "First Second"
    assert entry.url == "https://example.com/posts/1"
    assert entry.author == "Team"
    assert entry.tags == ("release",)
    assert entry.published_at == datetime(2026, 9, 22, 2, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize("version", ["1", "1.1"])
def test_json_feed_versions_preserve_plain_text_and_prefer_authors(version: str) -> None:
    data = {
        "version": f"https://jsonfeed.org/version/{version}",
        "title": "Example",
        "author": {"name": "Legacy"},
        "authors": [{"name": "Team"}],
        "items": [
            {
                "id": "2",
                "content_text": "literal <b>text</b>",
                "url": "../2",
                "date_published": "2026-09-22T06:00:00Z",
                "tags": ["update"],
            },
            {
                "id": "1",
                "title": "Old",
                "content_html": "<p>Hi<br>there</p>",
                "author": {"name": "Writer"},
            },
        ],
    }
    newest, older = parse_feed(json.dumps(data), source_url=SOURCE)
    assert newest.id == "2"
    assert newest.content == "literal <b>text</b>"
    assert newest.author == "Team"
    assert newest.url == "https://example.com/2"
    assert newest.published_at == datetime(2026, 9, 22, 6, tzinfo=UTC)
    assert older.author == "Writer"
    assert older.content == "Hi there"


def test_missing_id_uses_link_then_stable_full_content_digest() -> None:
    data = {
        "version": "https://jsonfeed.org/version/1.1",
        "items": [
            {"content_text": "Linked", "url": "../entry"},
            {"content_text": "Same text"},
            {"content_text": "x" * MAX_CONTENT_CHARS + "a"},
            {"content_text": "x" * MAX_CONTENT_CHARS + "b"},
        ],
    }
    first = parse_feed(json.dumps(data), source_url=SOURCE)
    second = parse_feed(json.dumps(data, indent=4), source_url=SOURCE)
    assert [entry.id for entry in first] == [entry.id for entry in second]
    assert first[0].id == "https://example.com/entry"
    assert first[1].id.startswith("sha256:")
    assert first[2].id != first[3].id
    assert len(first[2].content) == MAX_CONTENT_CHARS


def test_conflicting_duplicate_ids_keep_first_document_entry() -> None:
    entries = parse_feed(
        """<rss version="2.0"><channel>
      <item><guid isPermaLink="false">same</guid><description>First</description></item>
      <item><guid isPermaLink="false">same</guid><description>Second</description></item>
      <item><guid isPermaLink="false">other</guid><description>Other</description></item>
    </channel></rss>""",
        source_url=SOURCE,
    )
    assert [(entry.id, entry.content) for entry in entries] == [
        ("same", "First"),
        ("other", "Other"),
    ]


def test_atom_default_text_type_preserves_literal_markup() -> None:
    body = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>1</id><title>&lt;b&gt;Literal&lt;/b&gt;</title>
      <content>Use &lt;tag&gt; here.</content></entry></feed>"""
    (entry,) = parse_feed(body, source_url=SOURCE)
    assert entry.title == "<b>Literal</b>"
    assert entry.content == "Use <tag> here."


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com/a",
        "https://user:pass@example.com/a",
    ],
)
def test_non_http_or_credential_links_are_not_exposed(link: str) -> None:
    body = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1",
            "items": [
                {"id": "1", "content_text": "Entry", "url": link},
            ],
        }
    )
    assert parse_feed(body, source_url=SOURCE)[0].url == ""


@pytest.mark.parametrize("date", ["Tue, 22 Sep 2026 06:00:00", "2026-09-22T06:00:00"])
def test_naive_and_invalid_dates_remain_unknown(date: str) -> None:
    body = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1",
            "items": [
                {"id": "1", "content_text": "Entry", "date_published": date},
                {"id": "2", "content_text": "Entry", "date_published": "bad date"},
            ],
        }
    )
    first, second = parse_feed(body, source_url=SOURCE)
    assert first.published_at is None
    assert second.published_at is None


@pytest.mark.parametrize(
    "body",
    [
        '<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<rss version="2.0"><channel><item><description>&xxe;</description></item></channel></rss>',
        '<!DOCTYPE rss [<!ENTITY a "boom"><!ENTITY b "&a;&a;">]><rss><channel/></rss>',
        "<html><body>Not a feed</body></html>",
        '{"items": []}',
        '{"version":"https://jsonfeed.org/version/2","items":[]}',
        '{"version":"https://jsonfeed.org/version/1","items":[{"id":"1"}]}',
    ],
)
def test_unsafe_xml_and_non_feed_responses_fail(body: str) -> None:
    with pytest.raises(FeedParseError):
        parse_feed(body, source_url=SOURCE)


@pytest.mark.parametrize("format_name", ["rss", "atom", "json"])
def test_more_than_200_entries_fails_instead_of_silently_truncating(format_name: str) -> None:
    if format_name == "rss":
        body = (
            '<rss version="2.0"><channel>'
            + "<item><guid>same</guid></item>" * 201
            + "</channel></rss>"
        )
    elif format_name == "atom":
        body = (
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            + "<entry><id>same</id></entry>" * 201
            + "</feed>"
        )
    else:
        body = json.dumps(
            {
                "version": "https://jsonfeed.org/version/1",
                "items": [
                    {"id": "same", "content_text": "Same"},
                ]
                * 201,
            }
        )
    with pytest.raises(FeedParseError, match="exceeds 200 entries"):
        parse_feed(body, source_url=SOURCE)


def test_oversize_response_fails_and_projection_fields_are_bounded() -> None:
    with pytest.raises(FeedParseError, match="bytes"):
        parse_feed(" " * (MAX_FEED_BYTES + 1), source_url=SOURCE)
    body = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1",
            "items": [
                {
                    "id": "a" * 2000,
                    "title": "t" * 700,
                    "content_text": "c" * 13000,
                    "authors": [{"name": "n" * 400}],
                    "tags": [str(i) * 101 for i in range(40)],
                },
            ],
        }
    )
    (entry,) = parse_feed(body, source_url=SOURCE)
    assert entry.id.startswith("sha256:")
    assert len(entry.title) == 500
    assert len(entry.content) == MAX_CONTENT_CHARS
    assert len(entry.author) == 300
    assert len(entry.tags) == 32
    assert all(len(tag) <= 100 for tag in entry.tags)
