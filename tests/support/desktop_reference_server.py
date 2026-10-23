"""Current Desktop fixture: original container, routers and receipts, scripted paid model only."""

import argparse
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from tests.support.soul_reference_server import serve


async def publish_artifact(runtime, code):
    from tests.support.workspace_snapshots import snapshot_bytes

    from yuki.social.service import SocialContext

    device = await runtime.client_identity.pair(code["code"], "Fixture publisher")
    principal = await runtime.client_identity.authenticate(device["token"])
    incoming = await runtime.client_api.messages.submit(principal, str(uuid4()), "fixture file")
    artifact = snapshot_bytes(
        runtime.workspace_service.store, "desktop-reference.txt", b"Desktop reference artifact\n"
    )
    result = await runtime.social_service.execute(
        "send",
        {
            "artifact_id": artifact["artifact_id"],
            "attachment_kind": "file",
            "text": "Fixture artifact",
        },
        SocialContext(
            f"fixture-artifact:{uuid4()}",
            "file",
            principal.conversation_id,
            person_refs={"current_speaker": principal.person_id},
            trigger_event_id=incoming["event_id"],
            reply_presence_id=runtime.gateway_registry.resolve_by_handle(
                runtime.app_gateway.handle
            ).snapshot.presence_id,
        ),
    )
    assert result["status"] == "succeeded", result
    return {
        "artifact_id": artifact["artifact_id"],
        "artifact_version": artifact["sha256"],
        "artifact_event_id": result["event_id"],
        "artifact_source_event_id": incoming["event_id"],
        "artifact_conversation_id": principal.conversation_id,
    }


async def seed_work(runtime, artifact_state):
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.persistence.models import ChatEventModel
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_control import WorkControl
    from yuki.runtime.work_repository import WorkRepository
    from yuki.social.service import SocialContext

    database = runtime.database
    async with database.read_session() as reader:
        original = await reader.get(ChatEventModel, artifact_state["artifact_source_event_id"])
        conversation = await reader.get(
            CanonicalConversationModel, original.canonical_conversation_id
        )
        generation = conversation.generation
    repository = WorkRepository(database)
    lease = await repository.acquire(conversation.id, generation)
    assert lease is not None
    source = {
        "actor_person_id": original.author_person_id,
        "actor_user_id": original.author_account_id,
        "principal_kind": "person",
        "origin": "user_message",
        "trigger_event_id": original.id,
        "bot_user_id": "yuki-app",
        "presence_id": original.ingress_presence_id,
        "generation": generation,
        "conversation_id": conversation.id,
    }

    async def validate():
        assert await repository.valid(lease)

    source_key = f"desktop-fixture:{uuid4()}"
    control = WorkControl(repository, lease, source_key, source, validate)
    try:
        control.current = await repository.accept(
            lease, source_key=source_key, source=source, goal="Desktop reference work"
        )
        token = current_work_control.set(control)
        try:
            delivery = await runtime.social_service.execute(
                "send",
                {"text": "Which details should I use?"},
                SocialContext(
                    f"fixture-work:{uuid4()}",
                    "question",
                    conversation.id,
                    person_refs={"current_speaker": original.author_person_id},
                    trigger_event_id=original.id,
                    reply_presence_id=original.ingress_presence_id,
                ),
            )
            assert delivery["status"] == "succeeded", delivery
            row = await repository.get(control.current["id"])
            row = await repository.transition(lease, row["id"], row["revision"], "waiting_user")
        finally:
            current_work_control.reset(token)
        return {"work_id": row["id"]}
    finally:
        await repository.release(lease)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--base-path", default="/role/test")
    args = parser.parse_args()
    root, state = args.root.resolve(), args.state_file.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    soul = root / "character" / "SOUL.md"
    soul.parent.mkdir(parents=True, exist_ok=True)
    if not soul.exists():
        soul.write_text("你是 {{name}}。\n", encoding="utf-8")
    asyncio.run(serve(root, state, args.port, args.base_path.rstrip("/"), desktop=True))


if __name__ == "__main__":
    main()
