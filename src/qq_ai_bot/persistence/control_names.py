"""Human labels and QQ portraits for canonical owners in the operator UI."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.json_types import JsonObject
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    REDACTED_DISPLAY,
    ActivityView,
    ControlQueryError,
    DownloadView,
    sanitize_projected_display,
)
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.identity.db_models import (
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)

NAME_CAPABILITIES = {
    "person": "identity.binding.read",
    "space": "identity.space.read",
    "presence": "identity.presence.read",
    "conversation": "conversation.metadata.read",
    "binding": "identity.binding.read",
    "space_binding": "identity.space.read",
}


async def _people(session: AsyncSession, ids: tuple[str, ...]) -> dict[str, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                IdentityBindingModel.person_id,
                IdentityBindingModel.display_name,
                IdentityBindingModel.external_account_id,
            )
            .where(IdentityBindingModel.person_id.in_(ids))
            .order_by(IdentityBindingModel.updated_at.desc(), IdentityBindingModel.id.desc())
        )
    ).all()
    names: dict[str, str] = {}
    for owner, name, external in rows:
        if owner not in names and name:
            safe = sanitize_projected_display(name, external_ids=(external,), reveal=False)
            if safe != REDACTED_DISPLAY:
                names[owner] = safe
    return names


async def _spaces(session: AsyncSession, ids: tuple[str, ...]) -> dict[str, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(CanonicalSpaceModel.id, CanonicalSpaceModel.name).where(
                CanonicalSpaceModel.id.in_(ids)
            )
        )
    ).all()
    bindings = (
        await session.execute(
            select(
                SpaceBindingModel.space_id,
                SpaceBindingModel.display_name,
                SpaceBindingModel.external_space_id,
            )
            .where(SpaceBindingModel.space_id.in_(ids))
            .order_by(SpaceBindingModel.updated_at.desc(), SpaceBindingModel.id.desc())
        )
    ).all()
    externals: dict[str, list[str]] = {}
    for owner, _name, external in bindings:
        externals.setdefault(owner, []).append(external)
    names: dict[str, str] = {}
    for owner, name in rows:
        if not name:
            continue
        safe = sanitize_projected_display(name, external_ids=externals.get(owner, ()), reveal=False)
        if safe != REDACTED_DISPLAY:
            names[owner] = safe
    for owner, name, external in bindings:
        if owner not in names and name:
            safe = sanitize_projected_display(
                name, external_ids=externals.get(owner, (external,)), reveal=False
            )
            if safe != REDACTED_DISPLAY:
                names[owner] = safe
    return names


async def display_names(
    reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    references: JsonObject,
    allowed: frozenset[str],
) -> ActivityView:
    groups: dict[str, tuple[str, ...]] = {}
    for kind, ids in references.items():
        if kind not in NAME_CAPABILITIES or not isinstance(ids, (tuple, list)) or len(ids) > 100:
            raise ValueError("invalid name references")
        if any(type(item) is not str for item in ids):
            raise ValueError("invalid name references")
        groups[kind] = tuple(RequestId.parse(item).text for item in ids if type(item) is str)
    if sum(map(len, groups.values())) > 100:
        raise ValueError("too many name references")

    names: dict[str, str] = {}
    async with reader() as session:
        for kind, ids in groups.items():
            if kind not in allowed or not ids:
                continue
            if kind == "person":
                result = await _people(session, ids)
            elif kind == "space":
                result = await _spaces(session, ids)
            elif kind == "conversation":
                rows = (
                    await session.execute(
                        select(
                            CanonicalConversationModel.id,
                            CanonicalConversationModel.person_id,
                            CanonicalConversationModel.space_id,
                        ).where(CanonicalConversationModel.id.in_(ids))
                    )
                ).all()
                people = (
                    await _people(session, tuple(r.person_id for r in rows if r.person_id))
                    if "person" in allowed
                    else {}
                )
                spaces = (
                    await _spaces(session, tuple(r.space_id for r in rows if r.space_id))
                    if "space" in allowed
                    else {}
                )
                result = {
                    r.id: spaces.get(r.space_id)
                    or people.get(r.person_id)
                    or ("未命名群聊" if r.space_id else "未命名私聊")
                    for r in rows
                }
            elif kind == "presence":
                result = {
                    owner: "Yuki"
                    for owner in (
                        await session.scalars(
                            select(PresenceModel.id).where(PresenceModel.id.in_(ids))
                        )
                    ).all()
                }
            else:
                external_column = (
                    IdentityBindingModel.external_account_id
                    if kind == "binding"
                    else SpaceBindingModel.external_space_id
                )
                id_column = IdentityBindingModel.id if kind == "binding" else SpaceBindingModel.id
                name_column = (
                    IdentityBindingModel.display_name
                    if kind == "binding"
                    else SpaceBindingModel.display_name
                )
                result = {
                    owner: sanitize_projected_display(name, external_ids=(external,), reveal=False)
                    for owner, name, external in (
                        await session.execute(
                            select(id_column, name_column, external_column).where(
                                id_column.in_(ids)
                            )
                        )
                    ).all()
                    if name
                }
            for owner, name in result.items():
                if name:
                    safe = sanitize_projected_display(name, external_ids=(), reveal=False)
                    if safe != REDACTED_DISPLAY:
                        names[owner] = safe
    return ActivityView("display_names", {"names": names})


_QQ_ID = re.compile(r"[0-9]{1,20}\Z")
_MAX_AVATAR_BYTES = 1024 * 1024


async def download_avatar(
    reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    kind: str,
    owner_id: str,
) -> DownloadView:
    """Proxy bounded QQ image by canonical owner, never by client-supplied QQ URL."""
    if kind not in {"person", "space", "presence", "conversation"}:
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    owner_id = RequestId.parse(owner_id).text
    is_group = kind == "space"
    async with reader() as session:
        if kind == "conversation":
            row = (
                await session.execute(
                    select(
                        CanonicalConversationModel.person_id, CanonicalConversationModel.space_id
                    ).where(CanonicalConversationModel.id == owner_id)
                )
            ).first()
            if row is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            owner_id = row.space_id or row.person_id or ""
            is_group = bool(row.space_id)
            kind = "space" if is_group else "person"
        if kind == "space":
            rows = (
                (
                    await session.execute(
                        select(SpaceBindingModel.external_space_id)
                        .where(
                            SpaceBindingModel.space_id == owner_id,
                            SpaceBindingModel.platform == "qq",
                            SpaceBindingModel.status == "active",
                        )
                        .order_by(SpaceBindingModel.updated_at.desc(), SpaceBindingModel.id.desc())
                    )
                )
                .scalars()
                .all()
            )
        elif kind == "person":
            rows = (
                (
                    await session.execute(
                        select(IdentityBindingModel.external_account_id)
                        .where(
                            IdentityBindingModel.person_id == owner_id,
                            IdentityBindingModel.platform == "qq",
                            IdentityBindingModel.status == "active",
                        )
                        .order_by(
                            IdentityBindingModel.updated_at.desc(), IdentityBindingModel.id.desc()
                        )
                    )
                )
                .scalars()
                .all()
            )
        else:
            rows = (
                (
                    await session.execute(
                        select(PresenceModel.external_account_id).where(
                            PresenceModel.id == owner_id, PresenceModel.platform == "qq"
                        )
                    )
                )
                .scalars()
                .all()
            )
        external = next((value for value in rows if _QQ_ID.fullmatch(value)), None)
    if external is None:
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    url = (
        f"https://p.qlogo.cn/gh/{external}/{external}/100/" if is_group else "https://q1.qlogo.cn/g"
    )
    params = None if is_group else {"b": "qq", "s": "100", "nk": external}
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code != 200:
                    raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_AVATAR_BYTES:
                        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
                    chunks.append(chunk)
                content = b"".join(chunks)
    except httpx.HTTPError as exc:
        raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc
    media_type = (
        "image/png"
        if content.startswith(b"\x89PNG\r\n\x1a\n")
        else "image/jpeg"
        if content.startswith(b"\xff\xd8\xff")
        else "image/gif"
        if content.startswith((b"GIF87a", b"GIF89a"))
        else "image/webp"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        else None
    )
    if media_type is None:
        raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
    return DownloadView(name="avatar", content=content, media_type=media_type)
