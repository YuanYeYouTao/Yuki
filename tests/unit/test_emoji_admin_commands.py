"""Emoji diagnostics and original chat intents over SQLite and local media."""

import hashlib
import io
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import event, func, select
from tests.unit.test_group_route_recovery import message, stack

from yuki.admin.models import AdminActor
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.emoji.db_models import EmojiAssetModel, EmojiJobModel
from yuki.emoji.models import EmojiLifecycleStatus
from yuki.emoji.storage import EmojiStorage
from yuki.identity.db_models import CanonicalPersonModel
from yuki.services.commands.emoji import EmojiCommands
from yuki.vision.models import DownloadedMedia


def png(color):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, "PNG")
    return output.getvalue()


async def original(app, bot, text, key, *, attachments=()):
    request = replace(message(text, message_id=key), attachments=attachments)
    admitted = await app.canonical_ingress.pre_admit(bot, request)
    appended = await app.canonical_uow.append_inbound(admitted.message, admitted)
    request = replace(admitted.message, source_event_id=appended.event.id)
    actor = AdminActor(
        user_id=request.sender.user_id,
        person_id=request.person_id,
        is_superuser=True,
        trigger_message_id=request.message_id,
        trigger_event_id=request.source_event_id,
        conversation_key=request.conversation_id or "",
        space_id=request.space_id,
    )
    return request, actor


@pytest.fixture
async def emoji(database, tmp_path, monkeypatch):
    app, bot, *_ = await stack(database)
    storage = EmojiStorage(tmp_path / "emoji")
    app.emoji_admin._storage = app.emoji_collector._storage = storage
    content = png("red")
    resolver = AsyncMock(
        return_value=DownloadedMedia(
            content=content,
            content_type="image/png",
            content_hash=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
        )
    )
    monkeypatch.setattr(app.emoji_collector._resolver, "resolve", resolver)
    try:
        yield SimpleNamespace(
            app=app,
            bot=bot,
            service=app.emoji_admin,
            storage=storage,
            repository=app.emoji_admin._repository,
            resolver=resolver,
        )
    finally:
        await app.close()


async def asset(env, color="red"):
    request, actor = await original(env.app, env.bot, "fixture", f"seed-{color}")
    content = png(color)
    stored = env.storage.inspect(content, near_duplicate_enabled=False)
    env.storage.persist(content, stored)
    row, _ = await env.repository.record_candidate(
        stored,
        source_event_id=request.source_event_id,
        user_id=request.sender.user_id,
        group_id=request.group_id,
        person_id=request.person_id,
        space_id=request.space_id,
    )
    return row, actor


async def test_doctor_only_reads_and_repair_explicitly_changes_assets(emoji, database):
    missing, _ = await asset(emoji)
    preview, actor = await asset(emoji, "blue")
    emoji.storage.remove(missing.relative_path)
    emoji.storage.remove(preview.preview_relative_path)
    writes = []

    def count_write(_conn, _cursor, statement, *_args):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", count_write)
    try:
        assert await emoji.service.doctor() == (1, 1)
    finally:
        event.remove(engine, "before_cursor_execute", count_write)
    assert not writes
    assert (await emoji.repository.get(missing.id)).status is EmojiLifecycleStatus.CANDIDATE
    request, actor = await original(emoji.app, emoji.bot, "/ai admin emoji repair", "repair")
    rendered = await EmojiCommands(emoji.service).execute(
        actor=actor,
        message=request,
        argument="repair",
    )
    assert "标记原文件缺失 1 条" in rendered and "重建预览 1 条" in rendered
    assert (await emoji.repository.get(missing.id)).status is EmojiLifecycleStatus.MISSING
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EmojiJobModel)) == 1


async def test_old_adopt_replay_does_not_revive_later_unadopt(emoji):
    row, _ = await asset(emoji)
    await emoji.repository.set_status(row.id, EmojiLifecycleStatus.RECOGNIZED)
    old, actor = await original(emoji.app, emoji.bot, "/ai admin emoji adopt", "adopt")
    first = await emoji.service.mutate(actor, old, "adopt", identity=row.id)
    assert first.success
    newer, newer_actor = await original(emoji.app, emoji.bot, "/ai admin emoji unadopt", "unadopt")
    assert (await emoji.service.mutate(newer_actor, newer, "unadopt", identity=row.id)).success
    replay = await emoji.service.mutate(actor, old, "adopt", identity=row.id)
    assert replay.effective_state == first.effective_state
    assert not await emoji.repository.has_enabled_scope(row.id, scope_type="global", scope_id="")
    assert (await emoji.repository.get(row.id)).status is EmojiLifecycleStatus.RECOGNIZED


async def test_import_replay_does_not_download_or_reenqueue_completed_job(emoji, database):
    request, actor = await original(
        emoji.app,
        emoji.bot,
        "/ai admin emoji import",
        "import",
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "fixture"),),
    )
    first = await emoji.service.mutate(actor, request, "import")
    assert first.success
    async with database.immediate_session() as session:
        jobs = list(await session.scalars(select(EmojiJobModel)))
        assert len(jobs) == 1
        jobs[0].status = "completed"
    replay = await emoji.service.mutate(actor, request, "import")
    assert replay.effective_state == first.effective_state
    assert emoji.resolver.await_count == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EmojiJobModel)) == 1
        assert await session.scalar(select(EmojiAssetModel.seen_count)) == 1


async def test_unknown_import_effect_is_not_repeated(emoji, monkeypatch):
    effect = AsyncMock(side_effect=RuntimeError("transport acknowledgement lost"))
    monkeypatch.setattr(emoji.app.emoji_collector, "collect_attachment", effect)
    request, actor = await original(
        emoji.app,
        emoji.bot,
        "/ai admin emoji import",
        "unknown-import",
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "fixture"),),
    )
    first = await emoji.service.mutate(actor, request, "import")
    replay = await emoji.service.mutate(actor, request, "import")
    assert not first.success and not replay.success
    assert first.operation.status.value == replay.operation.status.value == "unknown"
    assert effect.await_count == 1


async def test_import_rechecks_person_after_media_wait(emoji, database):
    request, actor = await original(
        emoji.app,
        emoji.bot,
        "/ai admin emoji import",
        "revoke-import",
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "fixture"),),
    )
    downloaded = emoji.resolver.return_value

    async def revoke(*_args):
        async with database.immediate_session() as session:
            person = await session.get(CanonicalPersonModel, actor.person_id)
            person.is_superuser = False
        return downloaded

    emoji.resolver.side_effect = revoke
    result = await emoji.service.mutate(actor, request, "import")
    assert not result.success
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EmojiAssetModel)) == 0
    assert not emoji.storage.original_root.exists()
