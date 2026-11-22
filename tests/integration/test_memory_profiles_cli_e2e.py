"""E2E: the owner CLI ``yuki memory profile`` (memory layering P4, #227).

Authoritative spec: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md §9 (version
history and CLI), §4 (tables), §5.1/§5.2 (maintenance set and window), §5.3 (pinned
member lists), §7.4 (entry check), §8.3/§8.4 (one call, validation and version compare),
§13 H12, H16, H23, H24, H28, H33, H35 and §15.1 (first version generated offline).

Every command except ``generate`` runs the real ``yuki`` CLI in a child process against
the scene's real SQLite database. ``generate`` runs in process because its one model
call per profile is the only scripted part (the model runtime is swapped like the
task-scoped scan case X15); the plan file it writes is then applied through the child
process CLI. Dream's write of a profile is the data-layer call the selection step makes
(``ProfileStore.commit(..., require_unlocked=True)``). Each case writes normalized JSON
(CLI outputs, plan file, the three P4 tables; person and space IDs replaced by fixture
labels, times stripped) to ``test-results/memory-profiles/cli-<case>.json``; identical
input gives byte-identical artifacts. All fixture text is invented.

Failure cases, written before the implementation:
  H23 rollback rewrites or deletes an earlier version, does not lock, revives an entry
      whose fact became invalid, fails silently on an unknown version, writes on that
      error; ``diff`` shows unchanged entries or misses the removal; ``history`` misses a
      version, its source, reason, entry count or Dream run; ``show`` prints entry text
      without ``--content`` or omits it with ``--content``.
  H24/H35 ``lock`` leaves Dream's write possible or changes the entries; ``unlock`` keeps
      Dream out; lock/unlock on an unknown person exits 0.
  H28 ``generate`` writes any table row; a public call sees a non-public fact text; the
      private call's references are not the profile just generated; a person without
      available facts costs a model call; ``apply`` writes a profile whose signature or
      current version changed, rejects the whole plan for one bad profile, locks an
      unedited profile or leaves an edited one unlocked, applies the same plan twice.
  H11/H31 ``generate`` plans an output that cites a ``ref_N`` or unknown alias, exits 0
      when an output was void, or leaves the person's private-visible explicit facts out
      of the private call's references.
  H12 ``apply`` of a plan equal to the current entries adds a version.
  H16 ``members --pin`` stores more than five people, duplicates after root resolution or
      an unknown person, or does not resolve merged IDs to the root; Dream's replacement
      overwrites a pinned list; ``--unpin`` changes the people or keeps ``owner``.
  check  ``check`` misses an entry whose fact became invalid or lost eligibility, reports a
      clean profile, misses a successor mapping, exits 0 with violations or writes.
  status ``status`` misses the latest window, the maintenance set, a due profile, reports a
      locked or up-to-date profile as due, or omits a profile's version and lock.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.support.memory_profile_scene import ProfileScene
from tests.support.memory_resident_scene import run_cli
from tests.support.person_merge import merge_persons

from yuki.memory.enums import MemoryAuthority, MemoryInvalidationReason, MemorySourceType
from yuki.persistence.models import MemoryEvidenceModel

UNKNOWN = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
async def scene(database, tmp_path):
    return await ProfileScene(database, tmp_path).build()


async def _cli(scene: ProfileScene, *arguments: str, code: int = 0) -> dict[str, Any]:
    result = await run_cli(scene.database.url, "memory", "profile", *arguments)
    assert result.returncode == code, result
    return json.loads(result.stdout)


def _who(scene: ProfileScene, person: str, audience: str = "public") -> tuple[str, ...]:
    return ("--person", scene.persons[person], "--audience", audience)


def _label_order(scene: ProfileScene, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = scene.labels()
    return sorted(rows, key=lambda row: (labels[row["person_id"]], row["audience"]))


# ----------------------------------------------------------------------- H23
async def test_cli_h23_rollback_appends_locks_and_drops_invalid_entries(scene):
    """rollback --to 1 writes v4 (owner, locked) from v1's entries; the entry whose fact
    became invalid is dropped and listed; v1-v3 stay byte-identical."""
    g1 = await scene.event("space:G", "B", "我周末常去爬山，也爱拍照，还在学吉他", key="g1")
    f1 = await scene.fact("f1", "B", "周末常去爬山", evidence=(g1,))
    f2 = await scene.fact("f2", "B", "爱拍风景照", evidence=(g1,))
    f3 = await scene.fact("f3", "B", "在学吉他", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    e1 = scene.entry("周末常去爬山", f1, section="兴趣")
    e2 = scene.entry("爱拍风景照", f2)
    e3 = scene.entry("在学吉他", f3, section="兴趣")
    await store.commit(
        person,
        "public",
        (e1, e2),
        source="dream",
        reason="首版",
        expected_version=0,
        dream_run_public_id="run-1",
    )
    await store.commit(
        person,
        "public",
        (e1, e2, e3),
        source="dream",
        reason="加入吉他",
        expected_version=1,
        dream_run_public_id="run-2",
    )
    await store.commit(
        person,
        "public",
        (e3,),
        source="dream",
        reason="只留吉他",
        expected_version=2,
        dream_run_public_id="run-3",
    )
    assert await scene.chat._memories.invalidate_fact(
        f2, reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10002"
    )
    before = await scene.profile_tables()

    unknown = await _cli(scene, "rollback", *_who(scene, "B"), "--to", "9", code=1)
    after_unknown = await scene.profile_tables()
    rolled = await _cli(scene, "rollback", *_who(scene, "B"), "--to", "1")
    after = await scene.profile_tables()
    diff = await _cli(scene, "diff", *_who(scene, "B"), "--from", "1", "--to", "4")
    diff_text = await _cli(
        scene, "diff", *_who(scene, "B"), "--from", "2", "--to", "3", "--content"
    )
    history = await _cli(scene, "history", *_who(scene, "B"))
    show = await _cli(scene, "show", *_who(scene, "B"))
    show_text = await _cli(scene, "show", *_who(scene, "B"), "--content")
    no_profile = await _cli(scene, "show", *_who(scene, "B", "private"))

    assert unknown["error"] == "unknown_version" and after_unknown == before
    assert rolled["written"] is True and rolled["current_version"] == 4
    assert rolled["locked"] is True
    assert rolled["dropped"] == [{"index": 2, "section": None, "fact_ids": [f2], "failed": [f2]}]
    assert rolled["replaced"] == []
    versions = after["memory_profile_versions"]
    assert versions[:3] == before["memory_profile_versions"]
    assert (versions[3]["version"], versions[3]["source"], versions[3]["reason"]) == (
        4,
        "owner",
        "owner rollback to v1",
    )
    assert versions[3]["entries_json"] == [e1.to_json()]
    assert after["memory_profiles"][0]["locked"] == 1
    assert diff["added"] == [] and diff["rewritten"] == []
    assert diff["removed"] == [{"section": None, "fact_ids": [f2]}]
    assert diff["unchanged"] == 1
    assert diff_text["removed"] == [
        {"section": "兴趣", "fact_ids": [f1], "text": "周末常去爬山"},
        {"section": None, "fact_ids": [f2], "text": "爱拍风景照"},
    ]
    assert [
        (item["version"], item["source"], item["reason"], item["entries"])
        for item in history["versions"]
    ] == [
        (1, "dream", "首版", 2),
        (2, "dream", "加入吉他", 3),
        (3, "dream", "只留吉他", 1),
        (4, "owner", "owner rollback to v1", 1),
    ]
    assert [item["dream_run_public_id"] for item in history["versions"]] == [
        "run-1",
        "run-2",
        "run-3",
        None,
    ]
    assert show["current_version"] == 4 and show["source"] == "owner" and show["locked"]
    assert show["entries"] == [{"section": "兴趣", "fact_ids": [f1]}]
    assert "周末常去爬山" not in json.dumps(show, ensure_ascii=False)
    assert show_text["entries"] == [{"section": "兴趣", "fact_ids": [f1], "text": "周末常去爬山"}]
    assert no_profile["current_version"] == 0 and no_profile["entries"] == []
    scene.write(
        "cli-H23-rollback",
        {
            "facts": scene.facts,
            "rollback_unknown": unknown,
            "rollback": rolled,
            "diff_1_4": diff,
            "diff_2_3_content": diff_text,
            "history": history,
            "show": show,
            "show_content": show_text,
            "show_without_profile": no_profile,
            "tables": after,
        },
    )


# ------------------------------------------------------------------- H24 H35
async def test_cli_h24_h35_lock_keeps_dream_out_until_unlock(scene):
    """lock: the Dream write (require_unlocked) conflicts and nothing changes; unlock
    hands the profile back."""
    from yuki.memory.profiles import ProfileConflict

    g1 = await scene.event("space:G", "B", "我爱下围棋，也爱喝茶", key="g1")
    f1 = await scene.fact("f1", "B", "爱下围棋", evidence=(g1,))
    f2 = await scene.fact("f2", "B", "爱喝茶", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    first = (scene.entry("爱下围棋", f1),)
    second = (*first, scene.entry("爱喝茶", f2))
    await store.commit(person, "public", first, source="dream", reason="首版", expected_version=0)

    locked = await _cli(scene, "lock", *_who(scene, "B"))
    during = await scene.profile_tables()
    with pytest.raises(ProfileConflict) as dream:
        await store.commit(
            person,
            "public",
            second,
            source="dream",
            reason="加入喝茶",
            expected_version=1,
            require_unlocked=True,
        )
    after_dream = await scene.profile_tables()
    unknown = await _cli(scene, "lock", "--person", UNKNOWN, "--audience", "public", code=1)
    unlocked = await _cli(scene, "unlock", *_who(scene, "B"))
    written = await store.commit(
        person,
        "public",
        second,
        source="dream",
        reason="加入喝茶",
        expected_version=1,
        require_unlocked=True,
    )
    final = await scene.profile_tables()

    assert locked["locked"] is True and locked["current_version"] == 1
    assert dream.value.code == "locked" and after_dream == during
    assert during["memory_profiles"][0]["locked"] == 1
    assert unknown["error"] == "unknown_person"
    assert unlocked["locked"] is False and unlocked["current_version"] == 1
    assert written.written and written.profile.current_version == 2
    assert [row["version"] for row in final["memory_profile_versions"]] == [1, 2]
    scene.write(
        "cli-H24-H35-lock",
        {
            "facts": scene.facts,
            "lock": locked,
            "dream_while_locked": dream.value.code,
            "lock_unknown": unknown,
            "unlock": unlocked,
            "tables": final,
        },
    )


# ------------------------------------------------------------------- H28 H12
class _ProfileModel:
    """Scripted profile model: one entry per offered fact, in the offered order."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request) -> str:
        payload = json.loads(request.messages[-1].content)
        self.payloads.append(payload)
        entries = [
            {"section": None, "text": row["content"][:40], "facts": [row["ref"]]}
            for row in payload["facts"]
        ]
        return json.dumps({"entries": entries, "reason": "首版"}, ensure_ascii=False)


async def _generate(scene: ProfileScene, model, out: Path, capsys, **target):
    """Run ``generate`` in process with the scripted model runtime."""
    from tests.integration.test_execution_continuity_e2e import ScriptedProvider
    from tests.support.model_executor import InjectedModelExecutor

    import yuki.cli as cli

    provider = ScriptedProvider(model)

    @contextlib.asynccontextmanager
    async def scripted_models(settings, database):
        yield settings, InjectedModelExecutor(provider)

    original = cli._cli_models
    cli._cli_models = scripted_models
    capsys.readouterr()
    try:
        code = await cli._memory_command(
            scene.settings,
            argparse.Namespace(
                memory_command="profile",
                profile_command="generate",
                person=target.get("person"),
                audience=target.get("audience"),
                displayed=target.get("displayed", False),
                out=out,
                database_url=scene.database.url,
            ),
        )
    finally:
        cli._cli_models = original
    return code, json.loads(capsys.readouterr().out), len(provider.entered)


def _texts(payload: dict[str, Any], key: str) -> list[str]:
    return [row.get("content", row.get("text")) for row in payload.get(key, [])]


async def test_cli_h28_h12_generate_writes_nothing_and_apply_checks_each_profile(scene, capsys):
    """generate --displayed plans every maintained profile with available facts and
    writes no row; apply rejects the profile whose signature changed and the one whose
    version changed, writes the rest and locks only the one the owner edited."""
    g1 = await scene.event("space:G", "B", "我周末常去爬山，也爱拍风景照", key="g1")
    g2 = await scene.event("space:G", "C", "我在学做咖啡拉花", key="g2")
    for text in ("拉花练了一周", "今天拉出了一片叶子"):  # C ranks first in G, B second
        await scene.event("space:G", "C", text)
    b1 = await scene.event("private:B", "B", "我最近在偷偷学大提琴", key="b1")
    d1 = await scene.event("private:D", "D", "我在准备一场马拉松", key="d1")
    bp1 = await scene.fact("bp1", "B", "周末常去爬山", evidence=(g1,))
    bp2 = await scene.fact("bp2", "B", "爱拍风景照", evidence=(g1,))
    bv1 = await scene.fact("bv1", "B", "在偷偷学大提琴", evidence=(b1,))
    cp1 = await scene.fact("cp1", "C", "在学咖啡拉花", evidence=(g2,))
    dv1 = await scene.fact("dv1", "D", "在准备一场马拉松", evidence=(d1,))
    await scene.dream_run()
    store, persons = scene.profiles, scene.persons
    empty = await scene.profile_tables()
    model = _ProfileModel()
    plan_path = scene.tmp_path / "plan.json"

    code, generated, calls = await _generate(scene, model, plan_path, capsys, displayed=True)
    after_generate = await scene.profile_tables()
    plan = json.loads(plan_path.read_text("utf-8"))
    planned = {(row["person_id"], row["audience"]): row for row in plan["profiles"]}
    public_texts = [
        text
        for payload in model.payloads
        if payload["audience"] == "public"
        for text in _texts(payload, "facts") + _texts(payload, "reference")
    ]
    b_private = next(
        payload
        for payload in model.payloads
        if payload["audience"] == "private" and "在偷偷学大提琴" in _texts(payload, "facts")
    )
    status_before = await _cli(scene, "status")

    # Changes between generate and apply: C gains an available fact (signature), D's
    # private profile gets an owner version (current version); the owner edits B private.
    cp2 = await scene.fact("cp2", "C", "常带自己做的甜点", evidence=(g2,))
    await store.commit(
        persons["D"],
        "private",
        (scene.entry("在准备马拉松", dv1),),
        source="owner",
        reason="并发修改",
        expected_version=0,
    )
    edited = json.loads(plan_path.read_text("utf-8"))
    for row in edited["profiles"]:
        if (row["person_id"], row["audience"]) == (persons["B"], "private"):
            row["entries"] = [{"section": "兴趣", "text": "在学大提琴", "fact_ids": [bv1]}]
    plan_path.write_text(json.dumps(edited, ensure_ascii=False), "utf-8")
    applied = await _cli(scene, "apply", str(plan_path), code=1)
    after_apply = await scene.profile_tables()
    again = await _cli(scene, "apply", str(plan_path), code=1)
    after_again = await scene.profile_tables()

    # H12 through apply: a plan equal to the current entries adds no version.
    same_path = scene.tmp_path / "same.json"
    same_code, same_generated, _ = await _generate(
        scene, model, same_path, capsys, person=persons["B"], audience="public"
    )
    same = await _cli(scene, "apply", str(same_path))
    after_same = await scene.profile_tables()
    status_after = await _cli(scene, "status")

    assert code == 0 and empty == after_generate
    # The Dream run's own profile calls are unscripted here and void: they leave only
    # attempt rows (no version, no signature).
    assert empty["memory_profile_versions"] == []
    assert all(
        row["current_version"] == 0 and row["considered_signature"] is None
        for row in empty["memory_profiles"]
    )
    assert set(planned) == {
        (persons["B"], "public"),
        (persons["B"], "private"),
        (persons["C"], "public"),
        (persons["D"], "private"),
    }
    assert calls == generated["model_calls"] == 4
    assert {(row["person_id"], row["audience"]) for row in generated["skipped"]} >= {
        (persons["A"], "public"),
        (persons["D"], "public"),
        (persons["C"], "private"),
    }
    assert planned[(persons["B"], "public")]["entries"] == [
        {"section": None, "text": "周末常去爬山", "fact_ids": [bp1]},
        {"section": None, "text": "爱拍风景照", "fact_ids": [bp2]},
    ]
    assert all(row["entries"] == row["generated"] for row in plan["profiles"])
    assert all(row["base_version"] == 0 for row in plan["profiles"])
    assert "在偷偷学大提琴" not in public_texts and "在准备一场马拉松" not in public_texts
    assert _texts(b_private, "reference") == ["周末常去爬山", "爱拍风景照"]
    assert _label_order(scene, applied["applied"]) == [
        {
            "person_id": persons["B"],
            "audience": "private",
            "version": 1,
            "written": True,
            "edited": True,
            "locked": True,
        },
        {
            "person_id": persons["B"],
            "audience": "public",
            "version": 1,
            "written": True,
            "edited": False,
            "locked": False,
        },
    ]
    assert sorted(
        (scene.labels()[row["person_id"]], row["audience"], row["code"])
        for row in applied["rejected"]
    ) == [
        ("person:C", "public", "signature_changed"),
        ("person:D", "private", "version_changed"),
    ]
    profiles = {(row["person_id"], row["audience"]): row for row in after_apply["memory_profiles"]}
    assert profiles.get((persons["C"], "public"), {"current_version": 0})["current_version"] == 0
    assert profiles[(persons["B"], "private")]["locked"] == 1
    assert profiles[(persons["B"], "public")]["locked"] == 0
    assert (
        profiles[(persons["B"], "public")]["considered_signature"]
        == (planned[(persons["B"], "public")]["signature"])
    )
    versions = {
        (row["person_id"], row["audience"]): row for row in after_apply["memory_profile_versions"]
    }
    assert versions[(persons["B"], "private")]["entries_json"] == [
        {"section": "兴趣", "text": "在学大提琴", "fact_ids": [bv1]}
    ]
    assert versions[(persons["B"], "private")]["source"] == "owner"
    assert versions[(persons["D"], "private")]["reason"] == "并发修改"
    assert again["applied"] == [] and len(again["rejected"]) == 4
    assert {row["code"] for row in again["rejected"]} == {"signature_changed", "version_changed"}
    assert after_again == after_apply
    assert same_code == 0 and same_generated["model_calls"] == 1
    assert same["applied"][0]["written"] is False and same["applied"][0]["version"] == 1
    assert after_same["memory_profile_versions"] == after_apply["memory_profile_versions"]
    # status: the window of the latest Dream run and what is due.
    window = status_before["window"]
    assert window["dream_run_public_id"] is not None
    assert status_before["spaces"][scene.spaces["G"]] == [persons["C"], persons["B"], persons["A"]]
    assert status_before["spaces"][scene.spaces["H"]] == [persons["C"]]
    assert set(status_before["maintained"]["public"]) == {
        persons[key] for key in ("A", "B", "B2", "C", "D")
    }
    assert set(status_before["maintained"]["private"]) == {
        persons[key] for key in ("A", "B", "B2", "C", "D")
    }
    assert sorted(
        (scene.labels()[row["person_id"]], row["audience"]) for row in status_before["due"]
    ) == [
        ("person:B", "private"),
        ("person:B", "public"),
        ("person:C", "public"),
        ("person:D", "private"),
    ]
    assert sorted(
        (scene.labels()[row["person_id"]], row["audience"]) for row in status_after["due"]
    ) == [("person:C", "public"), ("person:D", "private")]
    by_profile = {(row["person_id"], row["audience"]): row for row in status_after["profiles"]}
    assert by_profile[(persons["B"], "private")]["locked"] is True
    assert by_profile[(persons["B"], "public")]["signature_current"] is True
    assert by_profile[(persons["D"], "private")]["current_version"] == 1
    assert cp2 > cp1
    scene.write(
        "cli-H28-generate-apply",
        {
            "facts": scene.facts,
            "generate": {
                **generated,
                "output": Path(generated["output"]).name,
                "skipped": _label_order(scene, generated["skipped"]),
            },
            "plan": {
                **plan,
                "profiles": _label_order(scene, plan["profiles"]),
            },
            "model_inputs": sorted(
                (
                    {
                        "audience": payload["audience"],
                        "current": payload["current"],
                        "facts": [
                            {"ref": row["ref"], "content": row["content"]}
                            for row in payload["facts"]
                        ],
                        "reference": payload.get("reference", []),
                    }
                    for payload in model.payloads
                ),
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
            "apply": {
                "applied": _label_order(scene, applied["applied"]),
                "rejected": _label_order(scene, applied["rejected"]),
            },
            "apply_again": {"rejected": _label_order(scene, again["rejected"])},
            "apply_unchanged": same,
            "status_before": _status_artifact(scene, status_before),
            "status_after": _status_artifact(scene, status_after),
            "tables": after_same,
        },
    )


def _status_artifact(scene: ProfileScene, status: dict[str, Any]) -> dict[str, Any]:
    labels = scene.labels()
    return {
        **status,
        "maintained": {
            key: sorted(labels[item] for item in value)
            for key, value in status["maintained"].items()
        },
        "due": _label_order(scene, status["due"]),
        "profiles": _label_order(scene, status["profiles"]),
        "member_lists": sorted(status["member_lists"], key=lambda row: labels[row["space_id"]]),
        "spaces": {
            labels[space]: [labels[item] for item in roots]
            for space, roots in sorted(status["spaces"].items(), key=lambda item: labels[item[0]])
        },
    }


async def test_cli_h11_h31_generate_voids_bad_outputs(scene, capsys):
    """An output citing an unknown alias or a ref_N alias is void: it is listed as
    rejected, left out of the plan, and generate exits 1; explicit facts visible in
    the private chat are references of the private call only."""
    g1 = await scene.event("space:G", "B", "我周末常去爬山", key="g1")
    b1 = await scene.event("private:B", "B", "我在学大提琴，早上别给我发消息", key="b1")
    await scene.fact("bp1", "B", "周末常去爬山", evidence=(g1,))
    await scene.fact("bv1", "B", "在学大提琴", evidence=(b1,))
    await scene.fact(
        "be1",
        "B",
        "早上别发消息",
        evidence=(b1,),
        source_type=MemorySourceType.EXPLICIT,
        authority=MemoryAuthority.EXPLICIT,
    )
    payloads: list[dict[str, Any]] = []

    def invalid(request) -> str:
        payload = json.loads(request.messages[-1].content)
        payloads.append(payload)
        cited = "ref_1" if payload["audience"] == "private" else "f_9"
        entry = {"section": None, "text": "一条条目", "facts": [cited]}
        return json.dumps({"entries": [entry], "reason": "首版"}, ensure_ascii=False)

    before = await scene.profile_tables()
    plan_path = scene.tmp_path / "invalid.json"
    code, generated, calls = await _generate(
        scene, invalid, plan_path, capsys, person=scene.persons["B"]
    )
    plan = json.loads(plan_path.read_text("utf-8"))
    public, private = payloads

    assert code == 1 and calls == 2 and plan["profiles"] == []
    assert [(row["audience"], row["code"]) for row in generated["rejected"]] == [
        ("public", "unknown_alias"),
        ("private", "reference_alias"),
    ]
    assert _texts(private, "reference") == ["早上别发消息"]
    assert "reference" not in public and "早上别发消息" not in _texts(public, "facts")
    assert await scene.profile_tables() == before
    scene.write(
        "cli-H11-H31-generate-void",
        {
            "facts": scene.facts,
            "generate": {**generated, "output": Path(generated["output"]).name},
            "model_inputs": [
                {
                    "audience": payload["audience"],
                    "facts": [
                        {"ref": row["ref"], "content": row["content"]} for row in payload["facts"]
                    ],
                    "reference": payload.get("reference", []),
                }
                for payload in payloads
            ],
        },
    )


# ----------------------------------------------------------------------- H16
async def test_cli_h16_members_pin_and_unpin(scene):
    """members --pin stores an owner list of roots that Dream leaves alone; --unpin
    hands the same people back to Dream."""
    async with scene.database.immediate_session() as writer:
        await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
    store, persons, spaces = scene.profiles, scene.persons, scene.spaces

    empty = await _cli(scene, "members", "--space", spaces["G"])
    pinned = await _cli(
        scene, "members", "--space", spaces["G"], "--pin", f"{persons['C']},{persons['B2']}"
    )
    after_pin = await scene.profile_tables()
    errors = {
        "duplicate_member": await _cli(
            scene,
            "members",
            "--space",
            spaces["G"],
            "--pin",
            f"{persons['B']},{persons['B2']}",
            code=1,
        ),
        "unknown_person": await _cli(
            scene, "members", "--space", spaces["G"], "--pin", UNKNOWN, code=1
        ),
        "too_many_members": await _cli(
            scene,
            "members",
            "--space",
            spaces["G"],
            "--pin",
            ",".join(persons[key] for key in ("A", "B", "C", "D", "A", "B")),
            code=1,
        ),
        "unknown_space": await _cli(
            scene, "members", "--space", "missing-space", "--pin", persons["A"], code=1
        ),
    }
    after_errors = await scene.profile_tables()
    dream = await store.replace_dream_members(
        {spaces["G"]: (persons["A"],), spaces["H"]: (persons["D"], persons["C"])},
        dream_run_public_id="run-1",
    )
    during_pin = await _cli(scene, "members", "--space", spaces["G"])
    unpinned = await _cli(scene, "members", "--space", spaces["G"], "--unpin")
    recomputed = await store.replace_dream_members(
        {spaces["G"]: (persons["A"],)}, dream_run_public_id="run-2"
    )
    final = await _cli(scene, "members", "--space", spaces["G"])

    assert empty["members"] is None
    assert pinned["members"]["person_ids"] == [persons["C"], persons["B"]]
    assert pinned["members"]["source"] == "owner"
    assert {code: payload["error"] for code, payload in errors.items()} == {
        code: code for code in errors
    }
    assert after_errors == after_pin
    assert dream == (spaces["H"],)
    assert during_pin["members"]["person_ids"] == [persons["C"], persons["B"]]
    assert unpinned["members"]["source"] == "dream"
    assert unpinned["members"]["person_ids"] == [persons["C"], persons["B"]]
    assert recomputed == (spaces["G"],)
    assert final["members"]["person_ids"] == [persons["A"]]
    assert final["members"]["dream_run_public_id"] == "run-2"
    scene.write(
        "cli-H16-members",
        {
            "empty": empty,
            "pin": pinned,
            "errors": errors,
            "during_pin": during_pin,
            "unpin": unpinned,
            "final": final,
            "tables": await scene.profile_tables(),
        },
    )


# --------------------------------------------------------------------- check
async def test_cli_check_reports_failed_ids_and_successors(scene):
    """check runs §7.4 on every stored profile: the entry with an invalid fact and the
    one that lost eligibility are listed with the failing IDs, a successor mapping is
    reported, clean profiles are not; it exits 1 and writes nothing."""
    g1 = await scene.event("space:G", "B", "我爱吃辣，周末打羽毛球，作息规律", key="g1")
    g2 = await scene.event("space:G", "C", "我在学做咖啡拉花", key="g2")
    b1 = await scene.event("private:B", "B", "我在准备搬家", key="b1")
    a1 = await scene.event("private:A", "A", "B 跟我说他最近作息很乱", key="a1")
    memories = scene.chat._memories
    x = await scene.fact("x", "B", "爱吃辣", evidence=(g1,))
    y = await scene.fact("y", "B", "口味偏重", evidence=(g1,))
    z = await scene.fact("z", "B", "周末打羽毛球", evidence=(g1,))
    w = await scene.fact("w", "B", "作息规律", evidence=(g1,))
    p = await scene.fact("p", "B", "在准备搬家", evidence=(b1,))
    c = await scene.fact("c", "C", "在学咖啡拉花", evidence=(g2,))
    store, persons = scene.profiles, scene.persons
    await store.commit(
        persons["B"],
        "public",
        (
            scene.entry("口味偏辣偏重", x, y, section="兴趣"),
            scene.entry("周末打羽毛球", z),
            scene.entry("作息规律", w),
        ),
        source="dream",
        reason="首版",
        expected_version=0,
    )
    await store.commit(
        persons["B"],
        "private",
        (scene.entry("在准备搬家", p),),
        source="dream",
        reason="首版",
        expected_version=0,
    )
    await store.commit(
        persons["C"],
        "public",
        (scene.entry("在学咖啡拉花", c),),
        source="dream",
        reason="首版",
        expected_version=0,
    )
    clean = await _cli(scene, "check")
    assert await memories.invalidate_fact(
        y, reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10002"
    )
    successor = await memories.correct_fact(z, content="周末常打羽毛球", actor_user_id="10002")
    assert successor is not None
    async with scene.database.immediate_session() as writer:
        writer.add(
            MemoryEvidenceModel(
                fact_id=w,
                event_id=a1,
                source_speaker_user_id="10001",
                relation="self_statement",
                authority="self_report",
                excerpt="作息",
                created_at=datetime.now(UTC),
            )
        )
    before = await scene.profile_tables()
    checked = await _cli(scene, "check", code=1)

    assert clean["violations"] == [] and clean["profiles"] == 3 and clean["entries"] == 5
    assert checked["profiles"] == 3 and checked["entries"] == 5
    assert [(row["person_id"], row["audience"]) for row in checked["violations"]] == [
        (persons["B"], "public")
    ]
    violation = checked["violations"][0]
    assert violation["dropped"] == [
        {"index": 1, "section": "兴趣", "fact_ids": [x, y], "failed": [y]},
        {"index": 3, "section": None, "fact_ids": [w], "failed": [w]},
    ]
    assert violation["replaced"] == [[z, successor.id]]
    assert "口味偏辣偏重" not in json.dumps(checked, ensure_ascii=False)
    assert await scene.profile_tables() == before
    scene.write(
        "cli-check",
        {
            "facts": {**scene.facts, "successor": successor.id},
            "clean": {**clean, "rendered": _label_order(scene, clean["rendered"])},
            "checked": {**checked, "rendered": _label_order(scene, checked["rendered"])},
        },
    )
