"""Current Desktop fixture: original container, routers and receipts, scripted paid model only."""

import argparse
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from tests.support.soul_reference_server import serve


def load_real_models(source: Path):
    """Read only the selected model catalog and credentials, never its runtime database."""
    import tomllib

    import tomlkit
    from dotenv import dotenv_values

    from yuki.model_runtime.models import ModelTask
    from yuki.model_runtime.pool import ModelClientPool
    from yuki.model_runtime.profiles import parse_model_profile_catalog
    from yuki.web.models import WebMode

    environment = {
        key: value for key, value in dotenv_values(source / ".env").items() if value is not None
    }
    path = Path(environment.get("MODEL_PROFILES_FILE", "webui-config/model_profiles.toml"))
    if not path.is_absolute():
        path = source / path
    # This opt-in acceptance uses one explicitly selected real Main connection;
    # no maintenance workers run and no old deployment routes are imported.
    document = tomllib.loads(path.read_text("utf-8"))
    main_id = document["routes"]["chat_agent"]
    document["profiles"] = {main_id: document["profiles"][main_id]}
    document["routes"] = {task.value: main_id for task in ModelTask}
    document.pop("search_backend", None)
    document.pop("search_connection", None)
    for profile in document["profiles"].values():
        profile["capabilities"] = [
            capability
            for capability in profile.get("capabilities", [])
            if capability != "native_web_search"
        ]
        profile.pop("search_mode", None)
        profile.pop("search_connection", None)
    catalog = parse_model_profile_catalog(
        tomlkit.dumps(document),
        environment=environment,
        web_mode=WebMode.DISABLED,
    )
    credentials = {
        profile.api_key_env: environment[profile.api_key_env]
        for profile in catalog.profiles.values()
        if profile.api_key_env and profile.api_key_env in environment
    }
    return catalog, ModelClientPool(secret_overrides=credentials)


class ControlledASR:
    """Replace only the paid provider; resolution, decoding and persistence stay real."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, audio):
        assert audio
        self.started.set()
        await self.release.wait()
        return "原語音轉寫：請把附件留下。"

    async def close(self):
        self.release.set()


async def seed_qq_entry(runtime, owner_id, bot):
    from datetime import UTC, datetime

    from yuki.domain.conversations import ScopeType
    from yuki.domain.messages import InboundMessage, SenderIdentity
    from yuki.identity.canonical_repository import (
        create_presence,
        ensure_account,
        find_identity_binding,
        replace_assignment,
    )
    from yuki.identity.db_models import CanonicalPersonModel

    now = datetime.now(UTC)
    async with runtime.database.immediate_session() as session:
        presence = await create_presence(session, external_account_id="8000", now=now)
        if await find_identity_binding(session, "1001") is None:
            account = await ensure_account(session, "1001", platform="qq", now=now)
            person = await session.get(CanonicalPersonModel, owner_id)
            await replace_assignment(session, account, None, person, now=now)
    runtime.gateway_registry.connect(bot, provider_id="snowluma", presence_id=presence.id)
    message = InboundMessage(
        ingress_key=str(uuid4()),
        native_message_id=str(uuid4()),
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001", nickname="Owner"),
        text="QQ 原入口的真實帳本內容",
        bot_user_id="8000",
    )
    admitted = await runtime.canonical_ingress.pre_admit(bot, message)
    assert admitted is not None and not admitted.dropped
    result = await runtime.canonical_uow.append_inbound(admitted.message, admitted)
    return {"event_id": result.event.id, "conversation_id": admitted.conversation_id}


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


async def seed_app_history(runtime, artifact_state, count):
    """Prepare accepted App history through original ingress, without scheduling Main."""
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.identity.db_models import IdentityBindingModel

    conversation_id = artifact_state["artifact_conversation_id"]
    async with runtime.database.read_session() as reader:
        conversation = await reader.get(CanonicalConversationModel, conversation_id)
        binding = await reader.get(IdentityBindingModel, conversation.identity_binding_id)
    ids = []
    for index in range(count):
        message = runtime.app_gateway.message(
            binding,
            str(uuid4()),
            f"歷史訊息 {index + 1}",
            (),
            conversation_id=conversation_id,
        )
        admitted = await runtime.canonical_ingress.pre_admit(runtime.app_gateway.handle, message)
        assert admitted is not None and not admitted.dropped
        result = await runtime.canonical_uow.append_inbound(admitted.message, admitted)
        ids.append(result.event.id)
    return {"conversation_id": conversation_id, "event_ids": ids}


def seed_avatar_inputs(root: Path) -> dict[str, str]:
    """Produce local image files; uploads still use the actual character API."""
    from PIL import Image, ImageDraw

    directory = root / "avatar-inputs"
    directory.mkdir(exist_ok=True)
    with Image.new("RGB", (4000, 3000)) as portrait:
        draw = ImageDraw.Draw(portrait)
        for box, color in (
            ((0, 0, 1999, 1499), "red"),
            ((2000, 0, 3999, 1499), "green"),
            ((0, 1500, 1999, 2999), "blue"),
            ((2000, 1500, 3999, 2999), "yellow"),
        ):
            draw.rectangle(box, fill=color)
        exif = Image.Exif()
        exif[274] = 6
        portrait.save(directory / "rotated.jpg", exif=exif, quality=95)
    with Image.new("RGB", (200, 200), "#e8354a") as small:
        small.save(directory / "small.png")
    with Image.new("RGB", (6000, 4000), "#35a85c") as large:
        large.save(directory / "large.jpg", quality=90)
    (directory / "broken.png").write_bytes(b"not an image")
    return {
        key: str(directory / filename)
        for key, filename in {
            "rotated": "rotated.jpg",
            "small": "small.png",
            "large": "large.jpg",
            "broken": "broken.png",
        }.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--base-path", default="/role/test")
    parser.add_argument("--real-model-source", type=Path)
    args = parser.parse_args()
    root, state = args.root.resolve(), args.state_file.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    soul = root / "character" / "SOUL.md"
    soul.parent.mkdir(parents=True, exist_ok=True)
    if not soul.exists():
        soul.write_text("你是 {{name}}。\n", encoding="utf-8")
    asyncio.run(
        serve(
            root,
            state,
            args.port,
            args.base_path.rstrip("/"),
            desktop=True,
            real_model_source=args.real_model_source.resolve() if args.real_model_source else None,
        )
    )


if __name__ == "__main__":
    main()
