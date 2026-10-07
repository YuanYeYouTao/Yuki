"""Real worker → Work → Social, with a downstream log outside the Bot database."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.support.codemode_cases import build_host, requires_worker, run_code
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.runtime.effect_outcomes import current_result_capture
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.source_keys import social_call_key

pytestmark = requires_worker


async def social_host(database, tmp_path, monkeypatch, *, lose_reply_at=None):
    social, owner, runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    snapshot = await chat._runtime_config.snapshot()
    downstream = tmp_path / "remote-operations.jsonl"
    original_call = social.bot.call_api
    count = 0

    async def transport(action, **params):
        nonlocal count
        if action in {"send_group_msg", "upload_group_file"}:
            capture = current_result_capture.get()
            assert capture is not None
            async with database.sessions() as reader:
                value = await reader.scalar(
                    select(effects.c.receipt_json).where(
                        effects.c.effect_key == capture.effect_key,
                    )
                )
                reference = json.loads(value)["invocation"]["original_domain_ref"]
                assert reference.startswith("social:")
                parent = await reader.get(SocialOperationModel, reference.removeprefix("social:"))
                assert parent is not None and parent.tool_call_id == social_call_key(
                    capture.effect_key
                )
            count += 1
            with downstream.open("a") as handle:
                handle.write(
                    json.dumps({"action": action, "params": params}, ensure_ascii=False) + "\n"
                )
            if count == lose_reply_at:
                raise OSError("downstream accepted, response lost")
        return await original_call(action, **params)

    monkeypatch.setattr(social.bot, "call_api", transport)

    async def domain(name, arguments):
        capture = current_result_capture.get()
        assert capture is not None
        token = current_work_control.set(owner.control)
        try:
            result = await social.service.execute(
                name,
                json.loads(arguments),
                replace(
                    social.context,
                    turn_id=owner.control.current["id"],
                    call_id=capture.effect_key,
                    trigger_event_id=runtime.trigger_event_id,
                    runtime_snapshot=snapshot,
                ),
            )
        finally:
            current_work_control.reset(token)
        return json.dumps({"ok": result["status"] == "succeeded", "data": result})

    case = build_host(owner, domain)
    return SimpleNamespace(case=case, social=social, downstream=downstream)


def remote_log(case):
    return (
        [json.loads(line) for line in case.downstream.read_text().splitlines()]
        if case.downstream.exists()
        else []
    )


@pytest.mark.parametrize("provider_id", ["code-1", "long-provider-" + "x" * 160])
async def test_two_equal_sends_bind_two_original_social_parents_and_reentry_only_queries(
    database, tmp_path, monkeypatch, provider_id
):
    env = await social_host(database, tmp_path, monkeypatch)
    code = "await yuki_send_message({'text': 'same'})\nawait yuki_send_message({'text': 'same'})"
    body, outer = await run_code(env.case, code, call_id=provider_id)
    assert body["status"] == "completed", body
    assert len(remote_log(env)) == 2
    repeated, _ = await run_code(env.case, code, call_id=provider_id)
    assert repeated == body and len(remote_log(env)) == 2
    async with database.sessions() as reader:
        values = list(
            await reader.scalars(
                select(effects.c.receipt_json).where(
                    effects.c.effect_key.like(outer.identity.operation_id + "/c%"),
                )
            )
        )
    refs = [json.loads(value)["invocation"]["original_domain_ref"] for value in values]
    assert len(refs) == len(set(refs)) == 2


@pytest.mark.parametrize("lose_reply_at", [None, 2])
async def test_lost_work_projection_queries_exact_social_vector_without_replaying(
    database, tmp_path, monkeypatch, lose_reply_at
):
    env = await social_host(database, tmp_path, monkeypatch, lose_reply_at=lose_reply_at)
    _, outer = await run_code(
        env.case, "await yuki_send_message({'text': 'one\\n\\ntwo\\n\\nthree'})"
    )
    key = outer.identity.operation_id + "/c0"
    async with database.sessions() as writer, writer.begin():
        raw = await writer.scalar(select(effects.c.receipt_json).where(effects.c.effect_key == key))
        prior = json.loads(raw)
        # Crash after durable Social receipts but before publishing the Work result.
        await writer.execute(
            update(effects)
            .where(effects.c.effect_key == key)
            .values(
                state="unknown",
                receipt_json=json.dumps(
                    {
                        "invocation": prior["invocation"],
                        "outcome": {
                            "tool": "send_message",
                            "side_effecting": True,
                            "uncertain": True,
                        },
                    }
                ),
            )
        )
    downstream_before = remote_log(env)
    observed = json.loads(await env.case.owner.journal.effect_result(key))
    assert observed["replay_forbidden"] and observed["work_effect_state"] == "unknown"
    assert observed["data"]["planned_messages"] == 3
    assert observed["data"]["parts"][0]["status"] == "succeeded"
    if lose_reply_at is None:
        assert observed["ok"] and observed["data"]["sent_messages"] == 3
    else:
        assert not observed["ok"] and observed["uncertain"]
        assert [p["status"] for p in observed["data"]["parts"]] == [
            "succeeded",
            "uncertain",
            "not_sent",
        ]
    assert remote_log(env) == downstream_before
    assert await env.case.control.has_unresolved_effects()


async def test_sequence_unknown_stops_remaining_parts_and_later_script_send(
    database, tmp_path, monkeypatch
):
    env = await social_host(database, tmp_path, monkeypatch, lose_reply_at=2)
    body, _ = await run_code(
        env.case,
        "await yuki_send_message({'text': 'one\\ntwo\\nthree'})\n"
        "await yuki_send_message({'text': 'never'})",
    )
    assert body["status"] != "completed" and len(remote_log(env)) == 2
    async with database.sessions() as reader:
        rows = list(await reader.scalars(select(SocialOperationModel)))
    parent = next(row for row in rows if row.action == "send_message_sequence")
    parts = [row for row in rows if row.action == "send_message"]
    assert parent.planned_parts == 3
    assert sorted(row.status for row in parts) == ["succeeded", "uncertain"]
    await run_code(
        env.case,
        "await yuki_send_message({'text': 'one\\ntwo\\nthree'})\n"
        "await yuki_send_message({'text': 'never'})",
    )
    assert len(remote_log(env)) == 2


async def test_uploaded_file_is_not_repeated_when_caption_response_is_lost(
    database, tmp_path, monkeypatch
):
    env = await social_host(database, tmp_path, monkeypatch, lose_reply_at=2)
    artifact = snapshot_bytes(env.social.store, "report.txt", b"offline report")
    args = {"artifact_id": artifact["artifact_id"], "attachment_kind": "file", "text": "caption"}
    code = f"await yuki_send_message({args!r})\nawait yuki_send_message({{'text': 'never'}})"
    body, _ = await run_code(env.case, code)
    assert body["status"] != "completed"
    await run_code(env.case, code)
    assert [entry["action"] for entry in remote_log(env)] == ["upload_group_file", "send_group_msg"]
    async with database.sessions() as reader:
        rows = list(await reader.scalars(select(SocialOperationModel)))
    assert next(row for row in rows if row.action == "send_message").status == "succeeded"
    assert next(row for row in rows if row.action == "send_file_caption").status == "uncertain"


async def test_stdout_return_and_no_reply_never_dispatch_social(database, tmp_path, monkeypatch):
    env = await social_host(database, tmp_path, monkeypatch)
    body, _ = await run_code(env.case, "print('sent already')\n'NO_REPLY'")
    assert body["status"] == "completed" and body["result"] == "NO_REPLY"
    assert remote_log(env) == []


async def test_cancel_between_parts_keeps_confirmed_first_part_and_blocks_next_dispatch(
    database, tmp_path, monkeypatch
):
    env = await social_host(database, tmp_path, monkeypatch)
    original = env.social.service.send_route
    cancelled = False

    async def cancel_before_next_route(*args, **kwargs):
        nonlocal cancelled
        if remote_log(env) and not cancelled:
            cancelled = True
            await env.case.control.repository.cancel(env.case.control.lease.conversation_id)
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.social.service, "send_route", cancel_before_next_route)
    with pytest.raises(WorkConflict):
        await run_code(env.case, "await yuki_send_message({'text': 'one\\ntwo\\nthree'})")
    assert len(remote_log(env)) == 1
    async with database.sessions() as reader:
        rows = list(await reader.scalars(select(SocialOperationModel)))
    parts = [row for row in rows if row.action == "send_message"]
    assert len([row for row in parts if row.status == "succeeded"]) == 1
    assert not any(row.status == "executing" for row in parts)
