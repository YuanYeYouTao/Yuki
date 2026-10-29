"""Selected file descriptors and original whole-transfer receipts use the same journal."""

import base64
import hashlib
import json
import os
import sys

import pytest

from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.files import CHUNK_BYTES, FileTransfers
from yuki_agent_connector.journal import Journal


@pytest.fixture
def transfers(tmp_path, monkeypatch):
    monkeypatch.setattr("yuki_agent_connector.journal.private_path", lambda *_args, **_kwargs: None)
    journal = Journal(tmp_path / "journal.sqlite3", namespace="original-device")
    yield FileTransfers(journal), journal
    journal.close()


def command(action, arguments, key="file-request"):
    return {"id": key, "resource": "file", "action": action, "arguments": arguments}


def write_chunk(transfer, path, content, offset, chunk, *, final):
    return command(
        "write",
        {
            "transfer_id": transfer,
            "path": str(path),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "offset": offset,
            "data_base64": base64.b64encode(chunk).decode(),
            "final": final,
        },
        key=f"{transfer}:write:{offset}",
    )


async def test_file_read_keeps_the_selected_fd_when_the_original_path_is_replaced(
    transfers, tmp_path
):
    files, _ = transfers
    path = tmp_path / "selected.bin"
    original = b"selected-original" * 10000
    path.write_bytes(original)
    metadata = await files.execute(command("stat", {"path": str(path)}, "selected-transfer"))
    try:
        replacement = tmp_path / "replacement.bin"
        replacement.write_bytes(b"new bytes at the old pathname")
        try:
            os.replace(replacement, path)
        except PermissionError:
            if sys.platform == "win32":
                pytest.skip("Windows Python open file handle does not grant delete/rename sharing")
            raise
        observed = bytearray()
        offset = 0
        while True:
            page = await files.execute(
                command(
                    "read",
                    {
                        "transfer_id": "selected-transfer",
                        "version": metadata["version"],
                        "offset": offset,
                    },
                )
            )
            observed.extend(base64.b64decode(page["data_base64"]))
            offset = page["next_offset"]
            if page["eof"]:
                break
        assert bytes(observed) == original
        assert path.read_bytes() != original
    finally:
        await files.close()


async def test_file_read_rejects_a_changed_selected_inode(transfers, tmp_path):
    files, _ = transfers
    path = tmp_path / "changing.bin"
    path.write_bytes(b"original")
    metadata = await files.execute(command("stat", {"path": str(path)}, "selected-transfer"))
    try:
        path.write_bytes(b"changed original bytes")
        with pytest.raises(DriverError, match="selected file changed"):
            await files.execute(
                command(
                    "read",
                    {
                        "transfer_id": "selected-transfer",
                        "version": metadata["version"],
                        "offset": 0,
                    },
                )
            )
    finally:
        await files.close()


async def test_chunked_upload_replays_the_original_receipt_without_touching_user_edits(
    transfers, tmp_path
):
    files, journal = transfers
    target = tmp_path / "saved.bin"
    content = b"x" * CHUNK_BYTES + b"last-selected-bytes"
    first = await files.execute(
        write_chunk("original-transfer", target, content, 0, content[:CHUNK_BYTES], final=False)
    )
    assert first["pending"] is True and not target.exists()
    result = await files.execute(
        write_chunk(
            "original-transfer", target, content, CHUNK_BYTES, content[CHUNK_BYTES:], final=True
        )
    )
    assert result["status"] == "succeeded" and target.read_bytes() == content
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    target.write_bytes(b"later user edit")
    restarted = FileTransfers(journal)
    replay = await restarted.execute(
        command("get", {"transfer_id": "original-transfer", "path": str(target)})
    )
    assert replay["status"] == "succeeded" and replay["sha256"] == result["sha256"]
    assert target.read_bytes() == b"later user edit"
    target.unlink()
    assert (
        await restarted.execute(
            command("get", {"transfer_id": "original-transfer", "path": str(target)})
        )
        == replay
    )
    encoded = journal.db.execute(
        "SELECT command FROM operations WHERE id=?", ("original-transfer",)
    ).fetchone()[0]
    assert "data_base64" not in encoded
    assert json.loads(encoded)["arguments"]["sha256"] == result["sha256"]
    with pytest.raises(DriverError, match="another path"):
        await restarted.execute(
            command(
                "get", {"transfer_id": "original-transfer", "path": str(tmp_path / "different.bin")}
            )
        )
    await files.close()
    await restarted.close()


async def test_upload_lookup_uses_the_original_requested_path_not_its_normalized_result(
    transfers, tmp_path, monkeypatch
):
    files, _ = transfers
    monkeypatch.chdir(tmp_path)
    requested = "./saved.txt"
    content = b"immutable original file effect"
    result = await files.execute(
        write_chunk("relative-transfer", requested, content, 0, content, final=True)
    )
    assert result["path"] != requested
    receipt = await files.execute(
        command("get", {"transfer_id": "relative-transfer", "path": requested})
    )
    assert receipt["status"] == "succeeded" and receipt["sha256"] == result["sha256"]
    await files.close()
