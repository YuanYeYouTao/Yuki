"""Deterministic schema export without a running Bot, database or credentials."""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from yuki import __version__
from yuki.client_api.http import ClientApi, attach_client_api


def client_openapi() -> dict[str, Any]:
    def unavailable() -> ClientApi:
        raise RuntimeError("schema export does not execute API handlers")

    app = FastAPI(title="Yuki personal API", version=__version__)
    attach_client_api(app, unavailable)
    return app.openapi()


def export_openapi(path: Path) -> None:
    path.write_text(
        json.dumps(client_openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
