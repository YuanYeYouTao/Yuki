"""Content-addressed immutable media in private on-disk protocol journals."""

from __future__ import annotations

from hashlib import sha256
from typing import Any


def externalize(value: Any, blobs: dict[str, bytes]) -> Any:
    if isinstance(value, str) and value.startswith("data:image/"):
        data = value.encode("utf-8")
        digest = sha256(data).hexdigest()
        blobs[digest] = data
        return {"$work_media": digest}
    if isinstance(value, dict):
        return {key: externalize(item, blobs) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [externalize(item, blobs) for item in value]
    return value


def hydrate(value: Any, blobs: dict[str, bytes]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$work_media"}:
            digest = value["$work_media"]
            data = blobs[digest]
            if sha256(data).hexdigest() != digest:
                raise ValueError("work_media_corrupt")
            return data.decode("utf-8")
        return {key: hydrate(item, blobs) for key, item in value.items()}
    if isinstance(value, list):
        return [hydrate(item, blobs) for item in value]
    return value


def references(value: Any) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$work_media"}:
            return {value["$work_media"]}
        return set().union(*(references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(references(item) for item in value))
    return set()
