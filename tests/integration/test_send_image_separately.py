"""E2E: a picture in a send is its own platform message; text is sent as usual.

Real ``SocialService`` with its durable receipts, ``PresenceRouter`` and the
built-in SnowLuma (OneBot) operations on a recording Bot; the Telegram case uses
the real Telegram operations over MockHTTP. Facts go to
``test-results/send-image-separately.json``.

Failure cases written before the implementation:

F1  text + picture still leaves as one QQ message (text segment and image segment
    in one ``send_group_msg``);
F2  picture then text: the picture message carries text or the text carries the
    picture, or the order changes;
F3  text + picture + text: the picture is not alone between the two texts;
F4  several pictures: a picture shares a message with text or another picture;
F5  reply and @: the quote or the @ is repeated on the picture message, or is
    missing from the first message (native segments and ledger facts);
F6  picture only: the single message changes (loses its quote, or becomes a
    two-part sequence);
F7  text only: plain text sends change (count, quote, normal line splitting);
F8  a failed or unknown later part is resent on replay, a confirmed part is sent
    again, or the replay reports fewer parts than the original receipts; when the
    first part fails the picture is still sent;
F9  the split parts do not each own a receipt under the same send call and the
    original Work execution (recovered result differs, or Work counts change);
F10 Telegram still sends the picture as a captioned photo;
F11 text with line breaks plus a picture is not split by the usual rule first.

Picture + text and text + picture + text cannot be written as one ``send`` (one
text and at most one file per call, text first), so F2/F3/F4 are the actual
consecutive calls of one turn.
"""

import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from tests.integration.test_native_social_delivery import native_env as native_fixture
from tests.integration.test_native_social_delivery import prepare_paid_send, sends
from tests.support.social_identity_cases import social_env
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.capabilities.results import normalize_legacy_result
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel
from yuki.social.repository import sequence_prefix

native_env = native_fixture

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "send-image-separately.json"
RESULTS: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_send_image_separately.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


def png(color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(stream, format="PNG")
    return stream.getvalue()


async def qq(database, tmp_path):
    env = await social_env(database, tmp_path)
    snapshot = await env.service.runtime_config.snapshot()
    env.context = replace(
        env.context,
        runtime_snapshot=replace(
            snapshot, reply=replace(snapshot.reply, delay_min_seconds=0, delay_max_seconds=0)
        ),
    )
    async with database.read_session() as reader:
        env.source = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    env.bot.calls.clear()
    return env


def picture(env, name: str, color: str) -> dict[str, str]:
    return {"artifact_id": snapshot_bytes(env.store, name, png(color))["artifact_id"]}


def messages(env) -> list[list[dict[str, Any]]]:
    return [params["message"] for action, params in env.bot.calls if action == "send_group_msg"]


def layout(env) -> list[list[str]]:
    """Each actual QQ message as its segment types, e.g. [["reply", "at", "text"], ["image"]]."""
    return [[segment["type"] for segment in message] for message in messages(env)]


async def outbound_events(env) -> list[dict[str, Any]]:
    async with env.db.read_session() as reader:
        rows = list(
            await reader.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.direction == "outbound")
                .order_by(ChatEventModel.id)
            )
        )
    return [
        {
            "content": row.content,
            "segments": sorted({item["type"] for item in json.loads(row.segments_json)}),
            "reply_to_event_id": row.reply_to_event_id,
            "origin_key": row.origin_key,
        }
        for row in rows
    ]


async def receipts(env) -> list[dict[str, Any]]:
    async with env.db.read_session() as reader:
        rows = list(
            await reader.scalars(select(SocialOperationModel).order_by(SocialOperationModel.id))
        )
    return [
        {
            "id": row.id,
            "turn": row.source_turn_id,
            "call": row.tool_call_id,
            "action": row.action,
            "status": row.status,
            "planned_parts": row.planned_parts,
            "platform_reference": row.platform_reference,
            "event_id": row.event_id,
        }
        for row in rows
    ]


def children(rows, turn: str, call: str) -> list[dict[str, Any]]:
    prefix = sequence_prefix(call)
    return sorted(
        (row for row in rows if row["turn"] == turn and row["call"].startswith(prefix)),
        key=lambda row: int(row["call"][len(prefix) :]),
    )


async def test_f1_text_and_picture_are_two_messages_with_own_receipts(database, tmp_path):
    env = await qq(database, tmp_path)
    args = {"text": "看这个", "file": picture(env, "meme.png", "red")}
    result = await env.service.execute("send", args, env.context)
    rows = await receipts(env)
    RESULTS["F1_text_then_picture"] = {
        "layout": layout(env),
        "result": result,
        "receipts": rows,
        "events": await outbound_events(env),
    }
    assert layout(env) == [["text"], ["image"]]
    assert messages(env)[0][0]["data"]["text"] == "看这个"
    assert result["status"] == "succeeded" and result["planned_messages"] == 2
    assert [part["status"] for part in result["parts"]] == ["succeeded", "succeeded"]
    parent = next(row for row in rows if row["call"] == env.context.call_id)
    assert parent["action"] == "send_message_sequence" and parent["planned_parts"] == 2
    parts = children(rows, env.context.turn_id, env.context.call_id)
    assert [row["status"] for row in parts] == ["succeeded", "succeeded"]
    assert len({row["platform_reference"] for row in parts}) == 2
    assert [event["segments"] for event in await outbound_events(env)] == [["text"], ["image"]]


async def test_f2_picture_then_text_keeps_order_and_separation(database, tmp_path):
    env = await qq(database, tmp_path)
    first = await env.service.execute(
        "send", {"file": picture(env, "a.png", "red")}, replace(env.context, call_id="p")
    )
    second = await env.service.execute("send", {"text": "然后"}, replace(env.context, call_id="t"))
    RESULTS["F2_picture_then_text"] = {"layout": layout(env), "results": [first, second]}
    assert layout(env) == [["image"], ["text"]]
    assert first["status"] == second["status"] == "succeeded"


async def test_f3_text_picture_text_leaves_picture_alone(database, tmp_path):
    env = await qq(database, tmp_path)
    first = await env.service.execute(
        "send",
        {"text": "前面", "file": picture(env, "a.png", "red")},
        replace(env.context, call_id="a"),
    )
    second = await env.service.execute("send", {"text": "后面"}, replace(env.context, call_id="b"))
    RESULTS["F3_text_picture_text"] = {"layout": layout(env), "results": [first, second]}
    assert layout(env) == [["text"], ["image"], ["text"]]
    assert [message[0]["data"].get("text") for message in messages(env)] == [
        "前面",
        None,
        "后面",
    ]


async def test_f4_several_pictures_are_each_one_message(database, tmp_path):
    env = await qq(database, tmp_path)
    first = await env.service.execute(
        "send",
        {"text": "两张", "file": picture(env, "a.png", "red")},
        replace(env.context, call_id="a"),
    )
    second = await env.service.execute(
        "send", {"file": picture(env, "b.png", "blue")}, replace(env.context, call_id="b")
    )
    RESULTS["F4_several_pictures"] = {"layout": layout(env), "results": [first, second]}
    assert layout(env) == [["text"], ["image"], ["image"]]


async def test_f5_reply_and_mention_stay_on_first_message_only(database, tmp_path):
    env = await qq(database, tmp_path)
    args = {
        "text": "@test-10001 看这个",
        "file": picture(env, "meme.png", "red"),
        "reply_to": env.source.id,
    }
    result = await env.service.execute("send", args, env.context)
    events = await outbound_events(env)
    RESULTS["F5_reply_and_mention"] = {
        "layout": layout(env),
        "messages": messages(env),
        "result": result,
        "events": events,
    }
    assert layout(env) == [["reply", "at", "text"], ["image"]]
    assert messages(env)[0][0] == {"type": "reply", "data": {"id": env.source.origin_key}}
    assert messages(env)[0][1] == {"type": "at", "data": {"qq": "10001"}}
    assert [event["reply_to_event_id"] for event in events] == [env.source.id, None]
    assert "mention" in events[0]["segments"] and "mention" not in events[1]["segments"]
    assert result["status"] == "succeeded"


async def test_f6_picture_only_is_unchanged_single_message(database, tmp_path):
    env = await qq(database, tmp_path)
    args = {"file": picture(env, "meme.png", "red"), "reply_to": env.source.id}
    result = await env.service.execute("send", args, env.context)
    rows = await receipts(env)
    RESULTS["F6_picture_only"] = {"layout": layout(env), "result": result, "receipts": rows}
    assert layout(env) == [["reply", "image"]]
    assert [row["action"] for row in rows] == ["send_message"]
    assert "parts" not in result and result["status"] == "succeeded"


@pytest.mark.parametrize("text,expected", [("只有文字", 1), ("第一句\n第二句", 2)])
async def test_f7_text_only_is_unchanged(database, tmp_path, text, expected):
    env = await qq(database, tmp_path)
    result = await env.service.execute(
        "send", {"text": text, "reply_to": env.source.id}, env.context
    )
    RESULTS[f"F7_text_only_{expected}"] = {"layout": layout(env), "result": result}
    assert layout(env) == [["reply", "text"]] + [["text"]] * (expected - 1)
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("failure", ["picture_failed", "picture_unknown", "text_failed"])
async def test_f8_replay_after_a_failed_part_sends_nothing_again(
    database, tmp_path, monkeypatch, failure
):
    env = await qq(database, tmp_path)
    original_call = env.bot.call_api
    failing = "image" if failure.startswith("picture") else "text"

    class Rejected(RuntimeError):
        uncertain = False

    async def fail_one(action, **params):
        if action == "send_group_msg" and params["message"][-1]["type"] == failing:
            env.bot.calls.append((action, params))
            if failure == "picture_unknown":
                raise httpx.ReadTimeout("lost acknowledgement")
            raise Rejected
        return await original_call(action, **params)

    monkeypatch.setattr(env.bot, "call_api", fail_one)
    args = {"text": "先发字", "file": picture(env, "meme.png", "red")}
    result = await env.service.execute("send", args, env.context)
    effects = list(env.bot.calls)
    original_receipts = await receipts(env)
    expected = {
        "picture_failed": ["succeeded", "failed"],
        "picture_unknown": ["succeeded", "uncertain"],
        "text_failed": ["failed", "prepared"],
    }[failure]
    assert [part["status"] for part in result["parts"]] == expected
    assert layout(env) == ([["text"]] if failure == "text_failed" else [["text"], ["image"]])
    # The same call again, first on a live connection and then disconnected.
    replayed = await env.service.execute("send", args, env.context)
    env.registry.disconnect(env.bot)
    offline = await env.service.execute("send", args, env.context)
    RESULTS[f"F8_replay_{failure}"] = {
        "layout": layout(env),
        "result": result,
        "replayed": replayed,
        "receipts": original_receipts,
    }
    assert replayed == offline == result
    assert env.bot.calls == effects
    assert await receipts(env) == original_receipts
    assert len(result["parts"]) == 2


async def test_f9_work_execution_recovers_each_part_from_original_receipts(
    database, tmp_path, monkeypatch
):
    from yuki.runtime.effect_outcomes import current_result_capture, execution_evidence
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_journal import WorkJournal
    from yuki.runtime.work_recovery_schema import deliveries

    env = await qq(database, tmp_path)
    original_call = env.bot.call_api

    class Rejected(RuntimeError):
        uncertain = False

    async def fail_picture(action, **params):
        if action == "send_group_msg" and params["message"][-1]["type"] == "image":
            env.bot.calls.append((action, params))
            raise Rejected
        return await original_call(action, **params)

    monkeypatch.setattr(env.bot, "call_api", fail_picture)
    image = picture(env, "meme.png", "red")
    args = {"text": "工作里发", "file": image}
    control, capture, context = await prepare_paid_send(env, args)
    context = replace(context, runtime_snapshot=env.context.runtime_snapshot)
    tokens = current_work_control.set(control), current_result_capture.set(capture)
    try:
        result = await env.service.execute("send", args, context)
    finally:
        current_result_capture.reset(tokens[1])
        current_work_control.reset(tokens[0])
    try:
        effects, sent = list(env.bot.calls), control.current["sent_messages"]
        rows = await receipts(env)
        async with env.db.read_session() as reader:
            intents = [
                dict(row)
                for row in (await reader.execute(select(deliveries).order_by(deliveries.c.id)))
                .mappings()
                .all()
            ]
        env.registry.disconnect(env.bot)
        recovered = json.loads(await WorkJournal(control.repository).effect_result(context.call_id))
        tokens = current_work_control.set(control), current_result_capture.set(capture)
        try:
            replayed = await env.service.execute("send", args, context)
        finally:
            current_result_capture.reset(tokens[1])
            current_work_control.reset(tokens[0])
        facts = [
            execution_evidence(
                normalize_legacy_result(json.dumps(response), provider_id="core", tool_name="send"),
                tool="send",
                side_effecting=True,
                arguments=json.dumps(args),
            )
            for response in ({"ok": False, "data": result}, recovered)
        ]
        RESULTS["F9_work_recovery"] = {
            "layout": layout(env),
            "result": result,
            "recovered": recovered,
            "receipts": rows,
            "work_deliveries": intents,
            "facts": facts,
        }
        assert layout(env) == [["text"], ["image"]]
        assert [part["status"] for part in result["parts"]] == ["succeeded", "failed"]
        parent = next(row for row in rows if row["call"] == context.call_id)
        assert parent["turn"] == control.current["id"] == context.turn_id
        assert len(children(rows, context.turn_id, context.call_id)) == 2
        for fact in facts:
            assert fact["delivered_artifacts"] == []
            assert fact["caption_delivered"] is True
        assert replayed == result
        assert env.bot.calls == effects and control.current["sent_messages"] == sent
        assert await receipts(env) == rows
    finally:
        await control.repository.release(control.lease)


async def test_f10_telegram_picture_is_not_a_captioned_photo(native_env):
    env = native_env
    image = snapshot_bytes(env.store, "meme.png", png("red"))
    args = {
        "text": "看这个",
        "file": {"artifact_id": image["artifact_id"]},
        "reply_to": env.event.id,
    }
    result = await env.service.execute("send", args, env.context)
    wire = [(method, sorted(params), sorted(files)) for method, params, files in sends(env)]
    RESULTS["F10_telegram"] = {"wire": wire, "result": result}
    assert [method for method, *_ in wire] == ["sendMessage", "sendPhoto"]
    assert "reply_parameters" in sends(env)[0][1]
    assert "caption" not in sends(env)[1][1] and "reply_parameters" not in sends(env)[1][1]
    assert result["status"] == "succeeded"


async def test_f11_text_lines_split_as_usual_before_picture(database, tmp_path):
    env = await qq(database, tmp_path)
    args = {"text": "第一句\n第二句", "file": picture(env, "meme.png", "red")}
    result = await env.service.execute("send", args, env.context)
    RESULTS["F11_lines_then_picture"] = {"layout": layout(env), "result": result}
    assert layout(env) == [["text"], ["text"], ["image"]]
    assert [message[0]["data"].get("text") for message in messages(env)] == [
        "第一句",
        "第二句",
        None,
    ]
