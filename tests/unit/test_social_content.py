from dataclasses import replace
from uuid import uuid4

import pytest

from yuki.adapters.onebot.social import OneBotSocialOperations
from yuki.gateway.providers.telegram_social import TelegramSocialOperations
from yuki.gateway.social import TransportTarget, split_transport_message
from yuki.social.content import (
    NameTarget,
    account_references,
    has_natural_mentions,
    prepare_content,
)


def target(name="Ice", *, account=None, assignment=None, ended=False):
    return NameTarget(
        account or str(uuid4()), "123", name, assignment or str(uuid4()), str(uuid4()), ended=ended
    )


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


def test_prefix_conflict_does_not_consume_body_or_pick_longest_account():
    content = prepare_content(
        "@A长名你看看", names=(("A", target("A")), ("A长名", target("A长名")))
    )
    assert content.message.text == "A长名你看看"
    assert content.message.mentions == ()
    assert len(content.unresolved[0]["candidates"]) == 2


def test_same_account_alias_prefix_keeps_longest_name():
    person = target("A")
    content = prepare_content("@A 长名你看看", names=(("A", person), ("A 长名", person)))
    assert content.message.text == "@A 长名你看看"
    assert content.message.mentions[0].text_end == len("@A 长名")


def test_ended_name_is_not_reassigned_but_explicit_account_is_physical():
    old = target(ended=True)
    physical = replace(old, assignment_id=None, person_id=None, ended=False)
    content = prepare_content("@Ice", names=(("Ice", old),))
    assert content.message.text == "Ice"
    explicit = prepare_content(
        f"@{{account:{old.account_id}}}", accounts={old.account_id: physical}
    )
    assert explicit.message.mentions[0].person_id is None
    assert explicit.message.mentions[0].assignment_id is None


def test_ambiguity_does_not_fall_back_to_telegram_username():
    content = prepare_content(
        "@Ice hello",
        names=(("Ice", target()), ("Ice", target())),
        native_mention=TelegramSocialOperations().native_mention,
    )
    assert content.message.text == "Ice hello"
    assert not content.message.mentions


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
    assert not has_natural_mentions(address)
    literal = prepare_content(address, names=(("Ice", person),))
    assert literal.message.text == address and not literal.message.mentions
    body = address + r" \@Ice，再@Ice"
    content = prepare_content(body, names=(("Ice", person),))
    assert content.message.text == address + " @Ice，再@Ice"
    assert len(content.message.mentions) == 1
    mention = content.message.mentions[0]
    assert content.message.text[mention.text_start : mention.text_end] == "@Ice"
    assert mention.text_end == len(content.message.text)


@pytest.mark.parametrize("prefix", ("a" * 10000, "中文" * 5000, ".+-" * 3000))
def test_long_nonaddress_token_keeps_a_following_native_mention(prefix):
    person = target()
    body = prefix + " @Ice"
    assert has_natural_mentions(body)
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
    assert account_references(body) == (person.account_id,)
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
