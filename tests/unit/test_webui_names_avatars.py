"""Canonical display labels and QQ portraits used by the operator UI."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.control_names import display_names, download_avatar
from qq_ai_bot.persistence.database import Database

NOW = datetime(2026, 9, 28, tzinfo=UTC)


async def _seed(database: Database) -> tuple[str, str, str, str]:
    person, space = str(uuid4()), str(uuid4())
    qq, group = str(uuid4().int % 10**12), str(uuid4().int % 10**12)
    async with database.sessions() as session, session.begin():
        session.add_all(
            [
                CanonicalPersonModel(
                    id=person,
                    enabled=True,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CanonicalSpaceModel(
                    id=space,
                    name="",
                    enabled=True,
                    autonomous_enabled=True,
                    require_mention=True,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
    async with database.sessions() as session, session.begin():
        session.add_all(
            [
                IdentityBindingModel(
                    id=str(uuid4()),
                    person_id=person,
                    platform="qq",
                    external_account_id=qq,
                    display_name="远野",
                    status="active",
                    revision=1,
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                SpaceBindingModel(
                    id=str(uuid4()),
                    space_id=space,
                    platform="qq",
                    external_space_id=group,
                    display_name="群名",
                    status="active",
                    revision=1,
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
    return person, space, qq, group


@pytest.mark.asyncio
async def test_names_use_canonical_bindings_and_space_name_fallback(database: Database) -> None:
    person, space, _, group = await _seed(database)
    from qq_ai_bot.persistence.control_query import ControlQueryAdapter

    adapter = ControlQueryAdapter(database)
    result = await display_names(
        adapter._reader,
        {"person": [person], "space": [space]},
        frozenset({"person", "space"}),
    )
    assert result.fields["names"] == {person: "远野", space: "群名"}
    limited = await display_names(
        adapter._reader,
        {"person": [person], "space": [space]},
        frozenset({"space"}),
    )
    assert limited.fields["names"] == {space: "群名"}
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == space)
            .values(name=f"群 {group}")
        )
    redacted = await display_names(adapter._reader, {"space": [space]}, frozenset({"space"}))
    assert redacted.fields["names"] == {space: "群名"}
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(SpaceBindingModel)
            .where(SpaceBindingModel.space_id == space)
            .values(display_name=f"绑定 {group}")
        )
    no_safe_name = await display_names(adapter._reader, {"space": [space]}, frozenset({"space"}))
    assert space not in no_safe_name.fields["names"]


@pytest.mark.asyncio
async def test_avatar_resolves_only_canonical_id_and_rejects_unsafe_image(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person, space, qq, group = await _seed(database)
    from qq_ai_bot.persistence.control_query import ControlQueryAdapter

    adapter = ControlQueryAdapter(database)
    calls: list[str] = []
    original = httpx.AsyncClient

    def make_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nportrait")

        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    result = await download_avatar(adapter._reader, "person", person)
    assert result.media_type == "image/png"
    assert calls == [f"https://q1.qlogo.cn/g?b=qq&s=100&nk={qq}"]
    await download_avatar(adapter._reader, "space", space)
    assert calls[-1] == f"https://p.qlogo.cn/gh/{group}/{group}/100/"
    with pytest.raises((ControlQueryError, ValueError)):
        await download_avatar(adapter._reader, "person", "12345678")
    assert len(calls) == 2

    def bad_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"<svg onload=alert(1)>")
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", bad_client)
    with pytest.raises(ControlQueryError):
        await download_avatar(adapter._reader, "person", person)
