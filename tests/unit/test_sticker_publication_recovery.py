"""Manual files use the original Manager publication owner and Social receipt."""

import asyncio
import io
import os
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from tests.support.social_identity_cases import social_env

from yuki.emoji.delivery import EmojiDeliveryService
from yuki.emoji.library import EmojiLibrary
from yuki.emoji.media import extension
from yuki.emoji.sticker_index import StickerIndex
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.service import WorkspaceService

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Actual Manager FDs require POSIX")
ARGS = {"text": "body", "emoji": {"goal": "happy"}}


@pytest.fixture
async def manual_library(database, tmp_path):
    env = await social_env(database, tmp_path)

    def manager():
        return PersistentManager(
            tmp_path / "manager", env.store, "test", "", "", tmp_path / "home", testing=True
        )

    owner = SimpleNamespace(manager=manager())

    class Bridge:
        async def execute(self, method, args, *, request_id):
            if method == "sticker_index":
                return await owner.manager.sticker_index(args)
            return await owner.manager.file_request(method, args, request_id)

    workspace = WorkspaceService(env.store)
    workspace.sandbox = Bridge()
    first_library, second_library = EmojiLibrary(workspace), EmojiLibrary(workspace)
    first = EmojiDeliveryService(env.service, first_library)
    second = EmojiDeliveryService(env.service, second_library)
    env.service.emoji_delivery = second
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "yellow").save(output, format="PNG")
    path = owner.manager.files.root / "stickers/qq/files/manual.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(output.getvalue())
    index = StickerIndex(owner.manager.files.root)
    index.catalog("qq", "stickers/qq/files/manual.png", tags=["happy"])
    index.rebuild()
    assert env.store.list()["items"] == []
    await env.service.execute("send", ARGS, env.context)
    intent = await first.intents.find(env.context.turn_id, env.context.call_id)
    try:
        yield SimpleNamespace(
            env=env,
            owner=owner,
            manager=manager,
            intent=intent,
            first=first,
            second=second,
            first_library=first_library,
            second_library=second_library,
        )
    finally:
        await first_library.close()
        await second_library.close()
        await owner.manager.close()


@pytest.mark.parametrize("restart", [False, True])
async def test_two_illustration_owners_share_actual_manual_file_publication(
    manual_library, monkeypatch, restart
):
    case = manual_library
    entered, release = threading.Event(), threading.Event()
    published = asyncio.Event()
    original_copy = case.owner.manager.file_operation
    original_publish = case.first_library.publish
    copies = []

    def blocked_copy(method, args):
        if method == "workspace_publish":
            copies.append(args["path"])
            entered.set()
            release.wait()
        return original_copy(method, args)

    async def stop_before_social(*args, **kwargs):
        result = await original_publish(*args, **kwargs)
        published.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(case.owner.manager, "file_operation", blocked_copy)
    monkeypatch.setattr(case.first_library, "publish", stop_before_social)
    first = asyncio.create_task(case.first._deliver(case.intent))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        second = asyncio.create_task(case.second._deliver(case.intent))
        completed, _ = await asyncio.wait((second,), timeout=0.25)
        assert not completed
        current = await case.second.intents.find(case.env.context.turn_id, case.env.context.call_id)
        assert not current.closed and current.selected is not None
        if restart:
            second.cancel()
        release.set()
        await asyncio.wait_for(published.wait(), 5)
        first.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        if restart:
            current = await case.second.intents.find(
                case.env.context.turn_id, case.env.context.call_id
            )
            assert not current.closed
            await case.second._deliver(current)
        assert (await case.second.project(case.env.context))["status"] == "succeeded"
        assert len(copies) == 1
        image_sends = [
            args
            for method, args in case.env.bot.calls
            if method.startswith("send_")
            and any(segment["type"] == "image" for segment in args.get("message", ()))
        ]
        assert len(image_sends) == 1
    finally:
        release.set()
        first.cancel()
        if second is not None:
            second.cancel()
        await asyncio.gather(
            first, *((second,) if second is not None else ()), return_exceptions=True
        )


async def test_manager_restart_publication_unknown_is_never_failed_or_replayed(
    manual_library, monkeypatch
):
    case = manual_library
    asset = await case.first_library.select("happy", platform="qq")
    intent = await case.first.intents.select(case.intent.id, asset.model_dump(mode="json"))
    publish_args = {
        "path": asset.relative_path,
        "expected_version": asset.sha256,
        "name": f"{asset.sha256}.{extension(asset.image_format)}",
        "artifact_id": asset.id,
        "source_key": f"sticker:{asset.platform}:{asset.sha256}",
    }

    def process_interrupted_before_copy(*_args):
        raise asyncio.CancelledError("process interrupted after durable request admission")

    monkeypatch.setattr(case.owner.manager, "file_operation", process_interrupted_before_copy)
    with pytest.raises(asyncio.CancelledError):
        await case.owner.manager.file_request(
            "workspace_publish", publish_args, f"sticker-publish:{intent.id}"
        )
    await case.owner.manager.close()
    case.owner.manager = case.manager()
    physical = []
    original_copy = case.owner.manager.file_operation

    def observe_copy(method, arguments):
        physical.append(method)
        return original_copy(method, arguments)

    monkeypatch.setattr(case.owner.manager, "file_operation", observe_copy)
    await case.second._deliver(intent)
    result = await case.second.project(case.env.context)
    assert result["status"] == "unknown"
    assert result["error"] == "file_mutation_unknown_read_current_version"
    assert not physical and case.env.store.list()["items"] == []
    assert await case.second.intents.pending() == ()
    replay = await case.env.service.execute("send", ARGS, case.env.context)
    assert replay["status"] == "succeeded" and replay["emoji"]["status"] == "unknown"
    assert sum(method.startswith("send_") for method, _ in case.env.bot.calls) == 1
