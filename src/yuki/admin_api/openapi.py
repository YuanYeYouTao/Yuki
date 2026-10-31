"""Export implemented management routes without starting the Bot or reading credentials."""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from yuki import __version__
from yuki.admin_api.http import AdminApi, attach_admin_api


def admin_openapi() -> dict[str, Any]:
    def unavailable() -> AdminApi:
        raise RuntimeError("schema export does not execute handlers")

    app = FastAPI(title="Yuki management API", version=__version__)
    attach_admin_api(app, unavailable)
    return app.openapi()


def export_openapi(path: Path) -> None:
    path.write_text(
        json.dumps(admin_openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
