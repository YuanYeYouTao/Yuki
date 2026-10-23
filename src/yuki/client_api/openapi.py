"""Export only implemented personal routes without starting the Bot."""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from yuki import __version__
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.version import CLIENT_API_REVISION
from yuki.external_agents.http import attach_external_agent_api
from yuki.external_agents.service import ExternalAgentService


def client_openapi() -> dict[str, Any]:
    def unavailable() -> ClientApi:
        raise RuntimeError("schema export does not execute handlers")

    app = FastAPI(title="Yuki personal API", version=__version__)
    attach_client_api(app, unavailable)

    def no_agents() -> ExternalAgentService:
        raise RuntimeError("schema export does not execute handlers")

    attach_external_agent_api(app, no_agents)
    document = app.openapi()
    document["info"]["x-yuki-api-revision"] = CLIENT_API_REVISION
    return document


def export_openapi(path: Path) -> None:
    path.write_text(
        json.dumps(client_openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
