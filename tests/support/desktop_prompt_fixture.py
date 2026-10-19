"""Opt-in loopback-only prompt faults use real files, settings and control receipts."""

import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from sqlalchemy import func, select

from yuki.container import ApplicationContainer
from yuki.identity.db_models import PresenceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel


def attach_prompt_fixture(
    app: FastAPI, container: ApplicationContainer, root: Path, model: FakeLLMProvider
) -> None:
    import yuki.character.prompts as module

    replace = module._replace
    marker = root / "crash-after-prompt-write"

    def crash_after_write(path, expected, content):
        replace(path, expected, content)
        if marker.exists():
            marker.unlink()
            os._exit(73)

    module._replace = crash_after_write

    @app.post("/__test/crash-after-prompt-write")
    async def arm_crash():
        marker.touch()
        return {"ok": True}

    @app.post("/__test/prompt-external/{key}")
    async def external_edit(key: str, request: Request):
        assert key in {"system_prompt", "persona"}
        path = root / f"{key}.md"
        path.write_bytes((await request.json())["content"].encode("utf-8"))
        return {"ok": True}

    @app.post("/__test/prompt-operator-reload")
    async def operator_reload():
        assert container.client_api.documents is not None
        return await container.client_api.documents.reload(str(uuid4()), operator_id=str(uuid4()))

    @app.get("/__test/prompt-facts")
    async def facts():
        async with container.database.sessions() as reader:
            count = await reader.scalar(select(func.count()).select_from(ChatEventModel))
            presences = [
                list(row)
                for row in (
                    await reader.execute(
                        select(
                            PresenceModel.id,
                            PresenceModel.platform,
                            PresenceModel.external_account_id,
                        ).order_by(PresenceModel.id)
                    )
                ).all()
            ]
        return {
            "presences": presences,
            "files": {
                key: {
                    "content": (root / f"{key}.md").read_bytes().decode("utf-8"),
                    "inode": (root / f"{key}.md").stat().st_ino,
                }
                for key in ("system_prompt", "persona")
            },
            "active": container.settings.active_prompt_revisions,
            "events": count,
            "prefixes": [str(request.messages[0].content) for request in model.requests],
        }
