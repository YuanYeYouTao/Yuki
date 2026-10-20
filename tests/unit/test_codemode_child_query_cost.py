"""Original composition recovery seeks its parent, not the Work's other effects."""

import json
import sqlite3
from contextlib import closing
from uuid import uuid4

from sqlalchemy import insert
from sqlalchemy.engine import make_url
from tests.support.social_identity_cases import social_env
from tests.support.sqlite_cost import capture_reads, read_vm_steps

from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, work


async def test_composition_children_seek_the_existing_parent_expression_index(database, tmp_path):
    environment = await social_env(database, tmp_path)
    identity, parent = str(uuid4()), "original-composition"
    receipt = {"invocation": {"version": 1, "parent_effect_key": parent}, "result": "original"}
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(work).values(
                id=identity,
                conversation_id=environment.context.conversation_id,
                generation=1,
                source_key="original-code-source",
                source_json='{"origin":"user_message"}',
                goal="original composition",
                state="completed",
                created=1.0,
                updated=1.0,
            )
        )
        await writer.execute(
            insert(effects).values(
                effect_key="original-child",
                work_id=identity,
                kind="tool",
                state="accepted",
                receipt_json=json.dumps(receipt),
                created=1.0,
                updated=1.0,
            )
        )
    costs = []
    previous = 0
    for size in (16, 640):
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(effects),
                [
                    {
                        "effect_key": f"unrelated-{index}",
                        "work_id": identity,
                        "kind": "tool",
                        "state": "accepted",
                        "receipt_json": json.dumps(
                            {"invocation": {"version": 1, "parent_effect_key": "other"}}
                        ),
                        "created": 1.0,
                        "updated": 1.0,
                    }
                    for index in range(previous, size)
                ],
            )
        with capture_reads(database) as statements:
            actual = await WorkRepository(database).composition_children(identity, parent)
        assert actual == [{"effect_key": "original-child", "state": "accepted", **receipt}]
        costs.append(read_vm_steps(database, statements))
        path = make_url(database.url).database
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
            plans = [
                str(row)
                for sql, parameters in statements
                for row in reader.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            ]
        assert any("ix_runtime_effects_parent" in plan for plan in plans), plans
        previous = size
    assert costs[1] <= costs[0] * 4, costs
