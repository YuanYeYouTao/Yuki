"""Media references remain internal IDs and never turn gateway text into a URL."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from qq_ai_bot.control_plane.media_types import raster_type
from qq_ai_bot.persistence.control_execution_query import _media_references
from qq_ai_bot.persistence.control_media_query import workspace_bytes
from qq_ai_bot.workspace.store import WorkspaceError


def test_outbound_media_references_are_only_original_internal_ids():
    artifact, emoji = str(uuid4()), str(uuid4())
    segments = [
        {"type": "image", "data": {"artifact_id": artifact, "file": "https://untrusted/image"}},
        {"type": "image", "data": {"emoji_id": emoji, "summary": "description"}},
        {"type": "image", "data": {"file": "https://untrusted/image"}},
        {"type": "file", "data": {"artifact_id": "../../secrets"}},
    ]
    assert _media_references(json.dumps(segments)) == (
        {"kind": "artifact", "id": artifact},
        {"kind": "emoji", "id": emoji},
    )
    assert _media_references("{malformed") == ()


def test_all_frontend_raster_extensions_have_byte_verified_types():
    assert raster_type(b"BM" + b"\0" * 24) == "image/bmp"
    assert raster_type(b"\0\0\0\x18ftypavif" + b"\0" * 12) == "image/avif"
    assert raster_type(b"\0\0\0\x18ftypavis" + b"\0" * 12) == "image/avif"
    assert raster_type(b"<script>bad</script>") == "application/octet-stream"


@pytest.mark.skipif(os.name != "posix", reason="FileWorkspace uses Linux directory descriptors")
def test_working_file_preview_is_content_typed_and_confined(tmp_path: Path):
    png = b"\x89PNG\r\n\x1a\n" + b"preview"
    (tmp_path / "photo.png").write_bytes(png)
    downloaded = workspace_bytes(tmp_path, "/workspace/photo.png")
    assert downloaded.content == png and downloaded.media_type == "image/png"
    assert raster_type(b"<script>bad</script>") == "application/octet-stream"
    with pytest.raises(WorkspaceError):
        workspace_bytes(tmp_path, "/workspace/../secrets")
    (tmp_path / "link.png").symlink_to(tmp_path / "photo.png")
    with pytest.raises((WorkspaceError, OSError)):
        workspace_bytes(tmp_path, "/workspace/link.png")
