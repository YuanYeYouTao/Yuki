"""Explicit, resumable initialization of an existing character's two avatar fields."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from yuki.admin_api.client import avatar_mutation, read
from yuki.character.avatar_cli import normalized_png
from yuki.services.media_resolver import _media_io


def _save(path: Path, state: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".avatar-upgrade-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_paths(state_file: Path, image: Path) -> None:
    if state_file.resolve() == image.resolve() or state_file.is_symlink():
        raise ValueError("select a separate persistent upgrade state file")


def _load(state_file: Path) -> dict[str, Any] | None:
    return json.loads(state_file.read_text()) if state_file.exists() else None


async def initialize_existing_avatar(
    client: httpx.AsyncClient, image: Path, state_file: Path, character_id: str
) -> dict[str, Any]:
    # This operator-only upgrade runs on the deployment host, never during Bot startup
    # or new-character setup. Serialize local invocations while the server retains CAS.
    import fcntl

    character_id = str(UUID(character_id))
    await _media_io(_validate_paths, state_file, image)
    descriptor = os.open(
        state_file.with_name(state_file.name + ".lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another avatar initialization is running") from error
        try:
            current = await read(client, "character")
            if current["id"] != character_id:
                raise ValueError("operator target is a different character")
            state = await _media_io(_load, state_file)
            if state is not None:
                if (
                    state.get("kind") != "existing-avatar-v1"
                    or state.get("character_id") != character_id
                ):
                    raise ValueError("upgrade state belongs to a different operation or character")
                if state.get("phase") not in {"prepared", "default_saved", "complete"}:
                    raise ValueError("upgrade state has an unknown phase")
                if state["phase"] == "complete":
                    return {"character_id": character_id, "status": "already_initialized"}
            else:
                if (
                    current["avatar_revision"] != 0
                    or current["avatar"]
                    or current["default_avatar"]
                ):
                    raise ValueError(
                        "avatar already changed; inspect it and use explicit CAS commands"
                    )
            png = await _media_io(normalized_png, image)
            digest = hashlib.sha256(png).hexdigest()
            if state is None:
                state = {
                    "kind": "existing-avatar-v1",
                    "character_id": character_id,
                    "sha256": digest,
                    "phase": "prepared",
                    "default_request": str(uuid4()),
                    "current_request": str(uuid4()),
                    "default_revision": 0,
                }
                await _media_io(_save, state_file, state)
            elif state["sha256"] != digest:
                raise ValueError(
                    "original upgrade image changed; retain the original request payload"
                )
            for action, key, phase in (
                ("set-default", "default", "default_saved"),
                ("set", "current", "complete"),
            ):
                if key == "default" and state["phase"] == "default_saved":
                    continue
                request = state[key + "_request"]
                revision = state[key + "_revision"]
                # A lost reply is queried under its original operator and UUID first.
                try:
                    receipt = await read(client, "operations/" + request)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:
                        raise
                else:
                    if (
                        receipt["status"] != "succeeded"
                        or receipt["result_resource_id"] != character_id
                    ):
                        raise ValueError("original avatar request is not confirmed successful")
                result = await avatar_mutation(
                    client, action, png, revision=revision, request_id=request
                )
                if result["id"] != character_id or result["avatar_revision"] != revision + 1:
                    raise ValueError("avatar receipt does not match the original upgrade")
                field = "default_avatar" if key == "default" else "avatar"
                if result[field]["sha256"] != digest:
                    raise ValueError("avatar receipt has different source pixels")
                state["phase"] = phase
                if key == "default":
                    state["current_revision"] = result["avatar_revision"]
                await _media_io(_save, state_file, state)
            return {"character_id": character_id, "status": "initialized"}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
