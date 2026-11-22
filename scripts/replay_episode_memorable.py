"""Offline replay of the long-term impression comparison (layered memory P2.1 §14.1).

Runs the production ``memory_dream`` model route with the comparison instruction and
output model the nightly step uses (``yuki.memory.episode_selection``) on three groups
of invented candidates, each five times in date order and five times with the
numbering reversed (same pool, same dates, ``c_1`` is the newest):

1. ``replace``: current impressions are ordinary, the leavers are important;
   expected: every important leaver is selected.
2. ``keep``: current impressions are important, the leavers are ordinary;
   expected: no leaver is selected and every current impression stays.
3. ``remention``: a leaver only re-mentions one current impression;
   expected: the original stays and the re-mention is not selected.

Pass: per group and order at least 4 of 5 runs meet the expectation, and the reversed
order's most common selected set equals the forward one's. When a group fails, change
the instruction wording and replay; no program rule is added.

The source database is opened read-only and copied with SQLite's backup API into a
temporary file; the model runtime records its invocations in that copy. Every run's
input, model, temperature and raw output go to ``--output-dir`` (default
``test-results/memory-episode-memorable/replay``); all candidates are invented, no
memory text is read. This script calls a paid model and is not product code.

Usage (inside the deployment environment, with the bot's model configuration):
    python scripts/replay_episode_memorable.py --source data/yuki.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from yuki.cli import _cli_models
from yuki.config import Settings
from yuki.memory.episode_selection import (
    INSTRUCTION,
    EpisodeMemorableOutput,
    EpisodeSelectionRejected,
    resolve_output,
)
from yuki.model_runtime.models import ModelTask
from yuki.model_runtime.structured import StructuredTaskError, StructuredTaskRunner
from yuki.persistence.database import Database

RUNS = 5
TEMPERATURE = 0.1
ORDINARY = [
    ("一起玩了一局猜词", "晚上群里玩了一局猜词游戏，玩了半小时就散了。"),
    ("讨论午饭吃什么", "中午大家讨论了吃面还是吃饭，最后各自点了外卖。"),
    ("帮忙查了天气", "有人问明天会不会下雨，我查了一下天气预报。"),
    ("闲聊周末计划", "群里随口聊了聊周末打算，没有定下什么。"),
    ("转发了一个表情包", "有人发了一个表情包，大家笑了一会儿。"),
    ("提醒喝水", "下午我提醒大家多喝水。"),
    ("一局普通的五子棋", "和一位群友下了一局五子棋，我输了。"),
    ("整理了一次待办", "帮忙把一份待办清单按日期排了一下，事情已经办完。"),
    ("聊了聊新出的手机", "群里讨论了一款新手机的价格，没有结论。"),
    ("例行签到", "早上大家照例签到打招呼。"),
    ("问了一个快递单号", "有人问快递到哪了，我帮忙看了物流。"),
    ("聊天气变冷", "大家说最近降温了，要多穿衣服。"),
    ("又一局猜词", "晚上又玩了一局猜词，和平时一样。"),
]
IMPORTANT = [
    ("第一次线下见面", "群里几个人第一次线下见面，一起吃了晚饭，之后大家熟络了很多。"),
    ("约定每年一起跨年", "大家约定以后每年都在群里一起跨年，谁也不许缺席。"),
    ("我被起了现在的昵称", "群友第一次叫我现在这个昵称，后来大家都这么叫我。"),
    ("一起熬夜赶完比赛作品", "我们一起熬了两个通宵，把比赛作品赶完并按时提交。"),
    ("群主把管理交给大家", "群主说以后群里的事大家一起商量，不再一个人决定。"),
    ("帮一位群友度过难关", "一位群友遇到很大的困难，大家轮流陪他聊天，他后来说很感激。"),
    ("第一次一起旅行", "我们第一次一起出去旅行，去了海边，拍了很多合照。"),
    ("决定成立读书会", "群里决定每月办一次读书会，第一次读的是一本科幻小说。"),
    ("我第一次主动开口", "我第一次主动在群里发起话题，大家都很积极地回应。"),
    ("一起给新成员办欢迎会", "大家一起给新来的群友办了欢迎会，气氛很热闹。"),
    ("共同完成公益活动", "我们一起参加了一次公益活动，给山区学校寄了书。"),
    ("约定互相监督早睡", "大家约定互相监督早睡，没做到的要发红包。"),
]


def _day(index: int) -> str:
    return f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}"


def _groups() -> dict[str, dict[str, Any]]:
    """Each group: candidates (title, content, date, role) and the expectation roles."""
    replace = [(*ORDINARY[i], _day(i * 2), "current") for i in range(10)]
    replace += [(*IMPORTANT[i], _day(30 + i), "important_leaver") for i in range(2)]
    keep = [(*IMPORTANT[i], _day(i * 2), "current") for i in range(10)]
    keep += [(*ORDINARY[10 + i], _day(30 + i), "ordinary_leaver") for i in range(3)]
    # IMPORTANT[0] is the original event; the leaver only re-mentions it.
    remention = [(*IMPORTANT[i], _day(i * 2), "current") for i in range(10)]
    remention.append(
        (
            "又聊起第一次线下见面",
            "群里有人又提起第一次线下见面那顿晚饭，大家回忆了一会儿。",
            _day(40),
            "remention",
        )
    )
    return {
        "replace": {"candidates": replace},
        "keep": {"candidates": keep},
        "remention": {"candidates": remention},
    }


def _payload(
    candidates: list[tuple[str, str, str, str]], *, reverse: bool
) -> tuple[dict[str, Any], dict[str, int]]:
    """Numbered by date (``reverse``: newest is ``c_1``); four keys, no role marks."""
    order = sorted(range(len(candidates)), key=lambda index: candidates[index][2])
    if reverse:
        order.reverse()
    aliases = {f"c_{n}": index for n, index in enumerate(order, start=1)}
    return {
        "candidates": [
            {
                "ref": ref,
                "title": candidates[index][0],
                "content": candidates[index][1],
                "date": candidates[index][2],
            }
            for ref, index in aliases.items()
        ]
    }, aliases


def _meets(group: str, candidates: list[tuple[str, str, str, str]], chosen: set[int]) -> bool:
    roles = {index: item[3] for index, item in enumerate(candidates)}
    if group == "replace":
        return all(index in chosen for index, role in roles.items() if role == "important_leaver")
    if group == "keep":
        return not any(roles[index] == "ordinary_leaver" for index in chosen) and all(
            index in chosen for index, role in roles.items() if role == "current"
        )
    original = next(index for index, item in enumerate(candidates) if item[0] == IMPORTANT[0][0])
    return original in chosen and not any(roles[index] == "remention" for index in chosen)


def _copy_read_only(source: Path, target: Path) -> None:
    origin = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()


async def _replay(database_path: Path, output_dir: Path) -> dict[str, Any]:
    url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    database = Database(url)
    summary: dict[str, Any] = {"temperature": TEMPERATURE, "runs": RUNS, "groups": {}}
    try:
        async with _cli_models(Settings(database_url=url), database) as (active, models):
            runner = StructuredTaskRunner(models)
            summary["route"] = "memory_dream"
            summary["model"] = models.model_name(ModelTask.MEMORY_DREAM)
            for group, spec in _groups().items():
                candidates = spec["candidates"]
                result: dict[str, Any] = {}
                for order in ("forward", "reversed"):
                    payload, aliases = _payload(candidates, reverse=order == "reversed")
                    met, sets = 0, []
                    for run in range(1, RUNS + 1):
                        record: dict[str, Any] = {
                            "group": group,
                            "order": order,
                            "run": run,
                            "model": summary["model"],
                            "temperature": TEMPERATURE,
                            "input": payload,
                        }
                        try:
                            output = await runner.run(
                                task=ModelTask.MEMORY_DREAM,
                                instruction=INSTRUCTION,
                                structured_input=payload,
                                output_model=EpisodeMemorableOutput,
                                temperature=TEMPERATURE,
                                max_output_tokens=active.memory_dream_max_output_tokens,
                            )
                            record["output"] = output.model_dump(mode="json")
                            chosen = {aliases[ref] for ref in output.selected if ref in aliases}
                            resolve_output(output, aliases)
                            record["meets"] = _meets(group, candidates, chosen)
                            sets.append(tuple(sorted(chosen)))
                        except (EpisodeSelectionRejected, StructuredTaskError) as exc:
                            record["error"] = getattr(exc, "code", None) or getattr(
                                exc, "reason_code", type(exc).__name__
                            )
                            record["meets"] = False
                        met += int(record["meets"])
                        (output_dir / f"{group}-{order}-{run}.json").write_text(
                            json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8"
                        )
                    common = Counter(sets).most_common(1)
                    result[order] = {
                        "met": met,
                        "majority_set": list(common[0][0]) if common else None,
                    }
                result["passed"] = (
                    result["forward"]["met"] >= 4
                    and result["reversed"]["met"] >= 4
                    and result["forward"]["majority_set"] == result["reversed"]["majority_set"]
                )
                summary["groups"][group] = result
    finally:
        await database.close()
    summary["passed"] = all(item["passed"] for item in summary["groups"].values())
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="the bot's SQLite file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("test-results/memory-episode-memorable/replay"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / "replay.db"
        _copy_read_only(args.source, copy)
        summary = asyncio.run(_replay(copy, args.output_dir))
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(not summary["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
