"""Offline resets rebind current SELF declarations without rewriting accepted versions."""

import json

from sqlalchemy import insert, select
from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.operations.reset_conversations import reset_all
from yuki.persistence.models import AutomationModel, AutomationVersionModel


async def test_offline_reset_is_idempotent_and_rebinds_only_the_current_self_generation(
    database, tmp_path
):
    case = await setup_run(database, tmp_path, principal="self", mode="silent")
    async with database.read_session() as reader:
        original = dict((await reader.execute(select(AutomationModel.__table__))).mappings().one())
        version = dict(
            (await reader.execute(select(AutomationVersionModel.__table__))).mappings().one()
        )
        conversation = await reader.get(
            CanonicalConversationModel, case.env.context.conversation_id
        )
        generation, floor = conversation.generation, conversation.last_event_id
    unrelated = {key: value for key, value in original.items() if key != "id"}
    other_authority = json.loads(version["authority_snapshot_json"])
    other_authority["conversation_generation"] = generation + 5
    unrelated.update(creation_source_key=None)
    async with database.immediate_session() as writer:
        other_id = (
            await writer.execute(
                insert(AutomationModel).values(**unrelated).returning(AutomationModel.id)
            )
        ).scalar_one()
        writer.add(
            AutomationVersionModel(
                automation_id=other_id,
                **{
                    key: value
                    for key, value in version.items()
                    if key not in {"id", "automation_id", "authority_snapshot_json"}
                },
                authority_snapshot_json=json.dumps(other_authority),
            )
        )

    preview = await reset_all(database, "offline-regression", apply=False)
    assert preview["newly_reset"] == preview["self_automations_rebound"] == 0
    applied = await reset_all(database, "offline-regression", apply=True)
    assert applied["newly_reset"] == applied["total"]
    assert applied["self_automations_rebound"] == 1
    repeated = await reset_all(database, "offline-regression", apply=True)
    assert repeated["already_reset"] == repeated["total"]
    assert repeated["newly_reset"] == repeated["self_automations_rebound"] == 0

    async with database.read_session() as reader:
        conversation = await reader.get(
            CanonicalConversationModel, case.env.context.conversation_id
        )
        assert conversation.generation == generation + 1
        assert conversation.starts_after_event_id == floor
        current = await case.repository.get(case.row.id, session=reader)
        assert current.authority_snapshot["conversation_generation"] == generation + 1
        other = await case.repository.get(other_id, session=reader)
        assert other.authority_snapshot == other_authority
        versions = list(
            (
                await reader.execute(
                    select(AutomationVersionModel.__table__)
                    .where(AutomationVersionModel.automation_id == case.row.id)
                    .order_by(AutomationVersionModel.version)
                )
            ).mappings()
        )
        assert len(versions) == 2 and dict(versions[0]) == version
        assert versions[1]["version"] == version["version"] + 1
        assert versions[1]["script_json"] == version["script_json"]
        assert json.loads(versions[1]["authority_snapshot_json"]) == current.authority_snapshot
