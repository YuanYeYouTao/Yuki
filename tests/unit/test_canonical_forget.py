"""Canonical Person erasure removes attribution and preserves shared sticker content."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from tests.support.canonical_identity import ensure_person

from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.database import Database
from yuki.persistence.people_repository import PeopleRepository


async def test_forget_keeps_shared_sticker_files_without_gallery_provenance(
    database: Database, tmp_path
):
    now = datetime(2026, 10, 19, tzinfo=UTC)
    async with database.sessions() as session, session.begin():
        person_id = await ensure_person(session, "1001", display_name="owner", now=now)
        other_id = await ensure_person(session, "1002", display_name="other", now=now)
    from PIL import Image

    directory = tmp_path / "workspace" / "stickers" / "qq"
    (directory / "files").mkdir(parents=True)
    (directory / "index").mkdir()
    original = directory / "files" / "shared.png"
    Image.new("RGB", (2, 2), "red").save(original)
    metadata = directory / "index" / "shared.json"
    metadata.write_text(json.dumps({"description": "共享的大笑图片", "tags": ["大笑"]}))
    files_before = {path: path.read_bytes() for path in (original, metadata)}

    assert await PeopleRepository(database).delete_person("1001") is True

    async with database.sessions() as session:
        assert int(await session.scalar(text("PRAGMA foreign_keys")) or 0) == 1
        assert await session.get(CanonicalPersonModel, person_id) is None
        assert await session.get(CanonicalPersonModel, other_id) is not None
        assert not any(
            "sticker" in name
            for name in (
                await session.scalars(text("SELECT name FROM sqlite_master WHERE type='table'"))
            )
        )
        assert tuple((await session.execute(text("PRAGMA foreign_key_check"))).all()) == ()
    assert {path: path.read_bytes() for path in files_before} == files_before
