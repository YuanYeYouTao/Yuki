"""Literal semantics and index candidates never invent source authorship."""

from __future__ import annotations

import sqlite3

import pytest

from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.persistence.history_search import (
    compile_match,
    fold_text,
    match_fields,
    namespace_terms,
    prepare_query,
    prepare_terms,
)


@pytest.mark.parametrize(
    ("keyword", "content", "mode", "expected"),
    [
        ("三", "三千", "all", True),
        ("三千", "三千", "all", True),
        ("電動車", "电动车", "phrase", True),
        ("乾", "乾坤", "all", True),
        ("於", "於世成", "all", True),
        ("ＡＢＣ", "abc", "all", True),
        ("café", "cafe\u0301", "all", True),
        ("a b", "ab", "phrase", False),
        ("a\n b", "a\t b", "phrase", True),
        ("京 电动车", "京", "any", True),
        ("京 电动车", "京", "all", False),
        ('NOT OR " * % _ -', 'NOT OR " * % _ -', "phrase", True),
        ("🙂", "你好🙂", "all", True),
    ],
)
def test_literal_semantics_and_fts_cover_true_matches(keyword, content, mode, expected):
    query = prepare_query(keyword, mode)
    actual = match_fields(query, content)
    assert (actual is not None) is expected
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE history USING fts5(tokens, content='', "
            "contentless_delete=1, tokenize='ascii', detail=none)"
        )
        conversation = "12345678-1234-4234-8234-123456789abc"
        connection.execute(
            "INSERT INTO history(rowid,tokens) VALUES(1,?)",
            (namespace_terms(conversation, prepare_terms(content)),),
        )
        candidates = connection.execute(
            "SELECT rowid FROM history WHERE history MATCH ?",
            (compile_match(conversation, query),),
        ).fetchall()
        if expected:
            assert candidates == [(1,)]
    finally:
        connection.close()


def test_fields_are_independent_and_reply_speech_is_not_evidence():
    speech = serialize_transcripts(
        (
            AudioTranscript("current", 2, "乙丙"),
            AudioTranscript("current", 5, "丁戊"),
            AudioTranscript("reply", 1, "只在回复中的原话"),
        )
    )
    assert match_fields(prepare_query("乙丙"), "甲", speech) == ("audio_transcript:2",)
    assert match_fields(prepare_query("丙丁", "phrase"), "甲", speech) is None
    assert match_fields(prepare_query("甲乙", "phrase"), "甲", speech) is None
    assert match_fields(prepare_query("回复"), "甲", speech) is None
    assert match_fields(prepare_query("segment_index source current"), "甲", speech) is None
    assert match_fields(prepare_query("甲 丁", "all"), "甲", speech) == (
        "content",
        "audio_transcript:5",
    )


def test_exclude_is_exact_even_when_grams_appear_in_different_fields():
    speech = serialize_transcripts((AudioTranscript("current", 1, "bcd"),))
    query = prepare_query("", exclude=("abcd", "  "))
    assert match_fields(query, "abc", speech) == ()
    assert match_fields(query, "abcd", speech) is None
    assert fold_text("Ａ\u030a\t B") == "å b"


def test_scope_prefix_and_short_terms_do_not_match_other_conversations():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE history USING fts5(tokens, content='', "
            "contentless_delete=1, tokenize='ascii', detail=none)"
        )
        other = "22345678-1234-4234-8234-123456789abc"
        target = "12345678-1234-4234-8234-123456789abc"
        connection.execute(
            "INSERT INTO history(rowid,tokens) VALUES(1,?)",
            (namespace_terms(other, prepare_terms("京")),),
        )
        assert (
            connection.execute(
                "SELECT rowid FROM history WHERE history MATCH ?",
                (compile_match(target, prepare_query("京")),),
            ).fetchall()
            == []
        )
    finally:
        connection.close()


def test_original_author_tags_share_one_text_match_and_family_union():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE history USING fts5(tokens, content='', "
            "contentless_delete=1, tokenize='ascii', detail=none)"
        )
        conversation = "12345678-1234-4234-8234-123456789abc"
        first = "12345678-1234-4234-8234-123456789111"
        second = "12345678-1234-4234-8234-123456789222"
        other = "12345678-1234-4234-8234-123456789333"
        for rowid, person, kind, body in (
            (1, first, "person", "稀有词"),
            (2, second, "person", "稀有词"),
            (3, other, "person", "稀有词"),
            (4, None, "yuki", "稀有词"),
        ):
            connection.execute(
                "INSERT INTO history(rowid,tokens) VALUES(?,?)",
                (
                    rowid,
                    namespace_terms(
                        conversation, prepare_terms(body), author_person_id=person, author_kind=kind
                    ),
                ),
            )
        family = compile_match(
            conversation,
            prepare_query("不存在 稀有词", "any"),
            person_ids=(first, second),
            author_kind="person",
        )
        assert connection.execute(
            "SELECT rowid FROM history WHERE history MATCH ? ORDER BY rowid", (family,)
        ).fetchall() == [(1,), (2,)]
        yuki = compile_match(conversation, prepare_query("稀有词"), author_kind="yuki")
        assert connection.execute(
            "SELECT rowid FROM history WHERE history MATCH ?", (yuki,)
        ).fetchall() == [(4,)]
    finally:
        connection.close()
