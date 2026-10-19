from dataclasses import replace
from uuid import uuid4

import pytest

from yuki.adapters.onebot.social import OneBotSocialOperations
from yuki.domain.messages import DISPLAY_NAME_LIMIT, sanitize_display_name
from yuki.gateway.providers.telegram_social import TelegramSocialOperations
from yuki.gateway.social import TransportTarget, split_transport_message
from yuki.social.content import (
    NameTarget,
    _name_matches,
    _positions,
    _render_content,
)


def target(name="Ice", *, account=None, assignment=None):
    return NameTarget(
        account or str(uuid4()), "123", name, assignment or str(uuid4()), str(uuid4())
    )


def prepare_content(text, *, names=(), accounts=None, native_mention=None):
    positions = _positions(text)
    return _render_content(
        text, positions, _name_matches(text, positions, names), accounts or {}, native_mention, {}
    )


def test_name_sanitizing_is_idempotent_when_the_length_boundary_is_a_space():
    original = "a" * (DISPLAY_NAME_LIMIT - 1) + " rest"
    name = sanitize_display_name(original)
    assert name == "a" * (DISPLAY_NAME_LIMIT - 1)
    assert sanitize_display_name(name) == name


def test_natural_mentions_keep_positions_and_repetition():
    ice, other = target(), target("查查")
    content = prepare_content(
        "先@Ice，你和@查查聊，@Ice看这里", names=(("Ice", ice), ("查查", other))
    )
    assert content.message.text == "先@Ice，你和@查查聊，@Ice看这里"
    assert [item.account_id for item in content.message.mentions] == [
        ice.account_id,
        other.account_id,
        ice.account_id,
    ]
    assert [
        content.message.text[item.text_start : item.text_end] for item in content.message.mentions
    ] == ["@Ice", "@查查", "@Ice"]


def test_longest_name_wins_before_accounts_are_compared():
    short, longest = target("A"), target("A长名")
    content = prepare_content("@A长名你看看", names=(("A", short), ("A长名", longest)))
    assert content.message.text == "@A长名你看看"
    assert content.message.mentions[0].account_id == longest.account_id
    assert content.message.mentions[0].text_end == len("@A长名")
    assert not content.unresolved


def test_same_account_alias_prefix_keeps_longest_name():
    person = target("A")
    content = prepare_content("@A 长名你看看", names=(("A", person), ("A 长名", person)))
    assert content.message.text == "@A 长名你看看"
    assert content.message.mentions[0].text_end == len("@A 长名")


def test_account_without_an_assignment_can_be_mentioned():
    physical = replace(target(), assignment_id=None, person_id=None)
    content = prepare_content("@Ice", names=(("Ice", physical),))
    assert content.message.text == "@Ice"
    assert content.message.mentions[0].account_id == physical.account_id
    explicit = prepare_content(
        f"@{{account:{physical.account_id}}}", accounts={physical.account_id: physical}
    )
    assert explicit.message.mentions[0].person_id is None
    assert explicit.message.mentions[0].assignment_id is None


def test_real_equal_names_default_to_stable_account_not_a_native_username():
    first, second = sorted((target(), target()), key=lambda item: item.account_id)
    content = prepare_content(
        "@Ice hello",
        names=(("Ice", second), ("Ice", first)),
        native_mention=TelegramSocialOperations().native_mention,
    )
    assert content.message.text == "@Ice hello"
    assert content.message.mentions[0].account_id == first.account_id
    assert not content.unresolved


def test_same_account_observed_in_old_null_and_current_stages_is_one_target():
    original = target()
    names = tuple(
        ("Ice", replace(original, assignment_id=assignment))
        for assignment in (str(uuid4()), None, original.assignment_id)
    )
    content = prepare_content("@Ice 再@Ice", names=names)
    assert content.message.text == "@Ice 再@Ice"
    assert [item.account_id for item in content.message.mentions] == [original.account_id] * 2
    assert not content.unresolved


@pytest.mark.parametrize("space", [" ", "\t", "\u3000", " \t "])
def test_horizontal_space_after_at_keeps_the_original_mention_span(space):
    person = target()
    body = "请@" + space + "Ice 看看"
    content = prepare_content(body, names=(("Ice", person),))
    assert content.message.text == body
    mention = content.message.mentions[0]
    assert mention.account_id == person.account_id
    assert body[mention.text_start : mention.text_end] == "@" + space + "Ice"


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\v", "\u2028"])
def test_space_after_at_does_not_skip_a_line_break(line_break):
    body = "@ " + line_break + "Ice"
    content = prepare_content(body, names=(("Ice", target()),))
    assert content.message.text == body
    assert not content.message.mentions


@pytest.mark.parametrize("reference", ["not-a-uuid", str(uuid4())])
def test_unavailable_explicit_account_preserves_the_original_at(reference):
    body = f"前@{{account:{reference}}} 后@Unknown"
    content = prepare_content(body)
    assert content.message.text == body
    assert not content.message.mentions
    assert content.unresolved == (
        {"text": f"@{{account:{reference}}}", "error": "account_unavailable"},
    )


def test_explicit_account_has_priority_over_a_same_text_name():
    person = target()
    body = f"@{{account:{person.account_id}}}"
    collision = target("{account:" + person.account_id + "}")
    content = prepare_content(
        body, names=((collision.display_name, collision),), accounts={person.account_id: person}
    )
    assert content.message.mentions[0].account_id == person.account_id


def test_escaped_and_address_account_references_are_not_looked_up():
    person = target()
    reference = f"@{{account:{person.account_id}}}"
    positions = _positions("https://example.test/" + reference + " \\" + reference)
    assert not any(item.account_id for item in positions)


def test_email_url_and_host_escape_do_not_become_group_names():
    content = prepare_content(r"x@Ice.com https://x/@Ice \@Ice @Ice", names=(("Ice", target()),))
    assert content.message.text == "x@Ice.com https://x/@Ice @Ice @Ice"
    assert len(content.message.mentions) == 1
    assert content.message.mentions[0].text_end == len(content.message.text)


@pytest.mark.parametrize(
    "address",
    (
        "user@Ice.com",
        "(first.last+tag-name@Ice.example)",
        "中文地址@Ice.com",
        "https://example.test/@Ice?redirect=@Ice",
        "http://user@Ice.example/path/@Ice",
    ),
)
def test_address_tokens_preserve_literal_at_and_the_next_natural_mention(address):
    person = target()
    assert not _positions(address)
    literal = prepare_content(address, names=(("Ice", person),))
    assert literal.message.text == address and not literal.message.mentions
    body = address + r" \@Ice，再@Ice"
    content = prepare_content(body, names=(("Ice", person),))
    assert content.message.text == address + " @Ice，再@Ice"
    assert len(content.message.mentions) == 1
    mention = content.message.mentions[0]
    assert content.message.text[mention.text_start : mention.text_end] == "@Ice"
    assert mention.text_end == len(content.message.text)


@pytest.mark.parametrize(
    "prefix", ("a" * 10000, "中文" * 5000, ".+-" * 3000), ids=["ascii", "cjk", "punctuation"]
)
def test_long_nonaddress_token_keeps_a_following_native_mention(prefix):
    person = target()
    body = prefix + " @Ice"
    assert _positions(body)[-1].name_start == len(prefix) + 2
    content = prepare_content(body, names=(("Ice", person),))
    assert content.message.text == body
    assert len(content.message.mentions) == 1
    assert content.message.mentions[0].text_start == len(prefix) + 1
    assert content.message.mentions[0].account_id == person.account_id


def test_unclosed_account_prefixes_do_not_hide_a_later_real_reference_or_name():
    person = target()
    malformed = "@{account:" * 1000
    reference = f"@{{account:{person.account_id}}}"
    body = malformed + " " + reference + " @Ice"
    assert [item.account_id for item in _positions(body) if item.account_id] == [person.account_id]
    content = prepare_content(body, names=(("Ice", person),), accounts={person.account_id: person})
    assert content.message.text == malformed + " @Ice @Ice"
    assert [mention.account_id for mention in content.message.mentions] == [
        person.account_id,
        person.account_id,
    ]
    assert all(
        content.message.text[mention.text_start : mention.text_end] == "@Ice"
        for mention in content.message.mentions
    )


def test_slicing_keeps_atomic_native_spans_and_shifts_offsets():
    content = prepare_content("12345@Ice67890@Ice", names=(("Ice", target()),)).message
    parts = split_transport_message(content, 8)
    assert "".join(part.text for part in parts) == content.text
    assert [
        part.text[item.text_start : item.text_end] for part in parts for item in part.mentions
    ] == ["@Ice", "@Ice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["qq", "telegram"])
async def test_wire_mentions_are_at_their_original_positions(platform):
    class Bot:
        def __init__(self):
            self.params = None

        async def call_api(self, method, **params):
            self.params = params
            return {"message_id": 1}

        async def call(self, method, params):
            self.params = params
            return {"message_id": 1}

    bot = Bot()
    operations = OneBotSocialOperations() if platform == "qq" else TelegramSocialOperations()
    content = prepare_content("前😀@Ice 后@Ice!", names=(("Ice", target()),)).message
    await operations.send(bot, TransportTarget("space", "42"), content)
    if platform == "qq":
        assert [part["type"] for part in bot.params["message"]] == [
            "text",
            "at",
            "text",
            "at",
            "text",
        ]
        assert bot.params["message"][0]["data"]["text"] == "前😀"
    else:
        assert (
            bot.params["text"]
            == '前😀<a href="tg://user?id=123">@Ice</a> 后<a href="tg://user?id=123">@Ice</a>!'
        )


@pytest.mark.parametrize(
    "body",
    [
        "前文。@Ice看看这个很长的句子！后面继续。" * 4,
        "- 开头\n前文@Ice\n\n后面一段需要保留。" * 3,
        "前文@Ice\n```text\nABCDEFGHIJ@Ice1234567890\n又一行@Ice\n```\n结尾@Ice",
    ],
)
def test_structured_splitting_keeps_native_occurrences_and_balanced_fences(body):
    from types import SimpleNamespace

    from yuki.services.message_splitter import OutboundMessageSplitter

    message = prepare_content(body, names=(("Ice", target()),)).message
    runtime = SimpleNamespace(reply=SimpleNamespace(hard_max_messages=100, max_qq_message_chars=24))
    parts = OutboundMessageSplitter.render_message(message, runtime=runtime)
    assert len([mention for part in parts for mention in part.mentions]) == body.count("@Ice")
    assert all(
        part.text[item.text_start : item.text_end] == "@Ice"
        for part in parts
        for item in part.mentions
    )
    assert all(part.text.count("```") in {0, 2} for part in parts)
    assert all(len(part.text) <= 24 for part in parts)
