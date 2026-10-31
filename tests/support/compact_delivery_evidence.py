"""Persist the actual compact scenario facts, including failed attempts."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

from yuki.conversation.observation_models import ContextObservationModel
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_schema_v1 import journal, work


@contextmanager
def compact_evidence(request):
    root = request.config.rootpath
    parent = (
        root / "test-results" / "compact" / hashlib.sha256(request.node.nodeid.encode()).hexdigest()
    )
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    evidence = {
        "nodeid": request.node.nodeid,
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "requests": [],
    }
    patch = subprocess.check_output(["git", "diff", "--", "src", "tests"], cwd=root)
    if patch:
        (directory / "source.patch").write_bytes(patch)
        evidence["source_patch_sha256"] = hashlib.sha256(patch).hexdigest()
    evidence["source_files"] = []
    for source in (Path(request.node.path).resolve(), Path(__file__).resolve()):
        relative = source.relative_to(root)
        destination = directory / "sources" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        evidence["source_files"].append(
            {
                "path": relative.as_posix(),
                "snapshot": destination.relative_to(directory).as_posix(),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )
    try:
        yield directory, evidence
    finally:
        (directory / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )


async def capture_compact_facts(database, directory, evidence):
    async with database.read_session() as reader:
        events = (await reader.scalars(select(ChatEventModel))).all()
        evidence["events"] = [
            {
                "id": row.id,
                "conversation_id": row.canonical_conversation_id,
                "direction": row.direction,
                "content": row.content,
            }
            for row in events
        ]
        evidence["compact_receipts"] = [
            {"source_key": row.source_key, "payload": json.loads(row.payload_json)}
            for row in (await reader.scalars(select(ContextObservationModel))).all()
        ]
        evidence["projections"] = [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in (await reader.scalars(select(PromptProjectionModel))).all()
        ]
        evidence["works"] = [dict(row) for row in (await reader.execute(select(work))).mappings()]
        evidence["journals"] = [
            dict(row) for row in (await reader.execute(select(journal))).mappings()
        ]
    protocol = Path(database.engine.url.database).parent / "work-protocol"
    if protocol.exists():
        shutil.copytree(protocol, directory / "work-protocol", dirs_exist_ok=True)
