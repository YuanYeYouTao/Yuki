"""Verify fixed distribution packaging inside a dedicated offline container.

Run as UID 10001 with the default Docker security policy, network disabled,
read-only root and a private /tmp tmpfs. No Bot or Provider is started. The
application role runs normal migrations on a new task-owned temporary DB.
"""

import ctypes
import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

import pydantic_monty

if len(sys.argv) != 2 or sys.argv[1] not in {"application", "validation"}:
    raise SystemExit("usage: verify_monty_packaging.py application|validation")
role = sys.argv[1]
root = Path("/opt/yuki-monty")
artifacts = json.loads((root / "artifacts.json").read_text())
audit = json.loads((root / "THIRD_PARTY_NOTICES.json").read_text())
for name in ("worker", "launcher"):
    path = root / ("monty" if name == "worker" else "monty-isolated")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifacts[name]["sha256"]
    assert path.stat().st_uid == 0 and not path.stat().st_mode & 0o022
assert audit["worker_sha256"] == artifacts["worker"]["sha256"]
assert audit["wheel_sha256"] == artifacts["binding"]["sha256"]
assert audit["source_sha"] == "3f9d6ef413fb951e5b80113b7088d535bd028fcb"
assert len(audit["packages"]) == 598
assert sum(p["archive_sha256"] is not None for p in audit["packages"]) == 580
assert audit["license_text_audit_complete"] is True
assert audit["license_text_gaps"] == []
notice_hashes = {notice["sha256"] for notice in audit["notice_texts"]}
for notice in audit["notice_texts"]:
    assert hashlib.sha256(notice["text"].encode()).hexdigest() == notice["sha256"]
for package in audit["packages"]:
    assert package["notices"]
    assert all(notice["sha256"] in notice_hashes for notice in package["notices"])
omissions = audit["upstream_notice_omissions"]
assert {item["package"] for item in omissions} == {"quote-use-0.8.4", "quote-use-macros-0.8.4"}
for item in omissions:
    assert item["declared_license"] == "MIT"
    assert item["copyright_notice_status"] == "not_supplied_by_upstream"
# The original MIT declarations are known. The standard text retains template
# placeholders; they are never claimed as package copyright attribution.
assert "b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5" in notice_hashes
assert pydantic_monty.__version__ == "1.0.1"
assert version("typing-extensions") == "4.16.0"
assert os.getuid() == 10001
failure = subprocess.run(
    [str(root / "monty-isolated"), "subprocess"],
    stdin=subprocess.DEVNULL,
    capture_output=True,
    timeout=10,
)
assert failure.returncode != 0
assert b"No permissions to create a new namespace" in failure.stderr
libc = ctypes.CDLL(None, use_errno=True)
assert libc.unshare(0x10000000) == -1 and ctypes.get_errno() == errno.EPERM
report = {
    "format": "yuki_monty_container_packaging_v1",
    "role": role,
    "uid": os.getuid(),
    "binding_version": pydantic_monty.__version__,
    "artifacts": artifacts,
    "locked_packages": len(audit["packages"]),
    "verified_registry_archives": sum(p["archive_sha256"] is not None for p in audit["packages"]),
    "target_packages": sum(p["in_worker_or_binding_target_tree"] for p in audit["packages"]),
    "notice_texts_verified": len(audit["notice_texts"]),
    "license_text_audit_complete": audit["license_text_audit_complete"],
    "license_text_gaps": len(audit["license_text_gaps"]),
    "upstream_notice_omissions": omissions,
    "default_namespace_policy": "denied_EPERM_fail_closed",
    "licenses": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "licenses").iterdir())
    },
    "bot_started": False,
    "external_model_calls": 0,
}
assert {"Yuki-LICENSE", "Monty-LICENSE", "TYPESHED-LICENSE"} <= report["licenses"].keys()
# Pi is a design reference. Its SDK/source is not part of this distribution.
assert "Pi-LICENSE" not in report["licenses"]
report["packaged_pi_license"] = False
if role == "application":
    from importlib.resources import files

    from qq_ai_bot.codemode.engine_monty import PinnedWorker
    from qq_ai_bot.services.agent_runner import AgentRunner

    assert AgentRunner is not None
    worker = PinnedWorker(
        root / "monty",
        artifacts["worker"]["sha256"],
        root / "monty-isolated",
        artifacts["launcher"]["sha256"],
    )
    worker.verify()
    assert not files("qq_ai_bot.agent_core").joinpath("Pi-LICENSE.txt").is_file()
    with tempfile.TemporaryDirectory(prefix="yuki-packaging-") as temporary:
        database_path = Path(temporary) / "qq_ai_bot.db"
        env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{database_path}")
        migration = subprocess.run(
            ["/app/.venv/bin/qq-ai-bot-cli", "init-db"], env=env, capture_output=True, timeout=60
        )
        if migration.returncode:
            raise AssertionError(migration.stderr.decode())
        with sqlite3.connect(database_path) as db:
            revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert revision == "0096"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assets = files("qq_ai_bot.webui").joinpath("assets")
    assert assets.joinpath("index.html").is_file()
    report.update(migration_head=revision, packaged_pi_license=False, built_webui=True)
report["verdict"] = "packaging_passed_container_isolation_unavailable"
print(json.dumps(report, indent=2))
