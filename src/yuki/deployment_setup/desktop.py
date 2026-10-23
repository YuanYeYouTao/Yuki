"""JSON entry points for the desktop's confirmed, local Compose setup.

Configuration and operator effects use the same services as ``yuki setup/admin``.
The caller passes secrets over stdin; this module never logs its input.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr

from yuki import __version__
from yuki.admin_api.client import admin_client, identity_mutation, mutate, read
from yuki.config import Settings
from yuki.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    build_model_profiles,
    commit_configuration,
    initialize_soul,
    prepare_operator_configuration,
    validate_configuration,
)
from yuki.web.models import WebMode


class DesktopConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    protocol: Literal["chat_completions", "responses", "anthropic_messages", "gemini"]
    base_url: str
    api_key: SecretStr = SecretStr("")
    model: str
    provider: str | None = None


def configure(paths: SetupPaths, body: DesktopConfiguration) -> dict[str, object]:
    if paths.env.is_file():
        return {"configured": True, "version": __version__, "preserved": True}
    document = EnvironmentDocument.load(paths)
    environment = document.values()
    environment.update(
        YUKI_VERSION=__version__,
        COMPOSE_PROFILES="",
        ONEBOT_ACCESS_TOKEN="",
        TELEGRAM_BOT_TOKEN="",
        FEISHU_APP_ID="",
        FEISHU_APP_SECRET="",
        LLM_BASE_URL=body.base_url,
        LLM_API_KEY=body.api_key.get_secret_value(),
        LLM_MODEL=body.model,
        MODEL_PROFILES_FILE="webui-config/model_profiles.toml",
        SOUL_FILE="character/SOUL.md",
        WEB_MODE="disabled",
        MEMORY_EMBEDDING_ENABLED="false",
        VISION_ENABLED="false",
    )
    operator = prepare_operator_configuration(paths, environment)
    profiles = (
        paths.model_profiles.read_text("utf-8")
        if paths.model_profiles.is_file()
        else build_model_profiles(
            main_protocol=body.protocol,
            main_provider=body.provider,
            flash_enabled=False,
            environment=environment,
            web_mode=WebMode.DISABLED,
            main_api_key_env="LLM_API_KEY" if body.api_key.get_secret_value() else "",
        )
    )
    configuration = SetupConfiguration(
        environment,
        profiles,
        None,
        write_model_profiles=not paths.model_profiles.is_file(),
        operator_policy=operator,
    )
    source = paths.soul if paths.soul.exists() else paths.soul_template
    validate_configuration(
        paths,
        replace(configuration, environment={**environment, "SOUL_FILE": str(source.resolve())}),
    )
    for directory in ("data", "character", "config", "webui-config", "workspace", "plugins"):
        (paths.root / directory).mkdir(parents=True, exist_ok=True)
    initialize_soul(paths)
    commit_configuration(paths, document, configuration)
    return {"configured": True, "version": __version__}


class DesktopPairing(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    display_name: str
    person_request_id: UUID
    pair_request_id: UUID
    owner_request_id: UUID | None = None
    person_id: UUID | None = None


async def pair(body: DesktopPairing) -> dict[str, object]:
    # This command runs inside the selected Bot, not the desktop renderer. The
    # existing setup owns this operator credential and the admin client owns HTTP.
    os.environ["YUKI_CONTROL_CREDENTIAL"] = os.environ["YUKI_CONTROL_OPERATOR_TOKEN"]
    async with admin_client(Settings()) as client:
        if body.person_id is None:
            created = await mutate(
                client,
                "persons",
                0,
                request_id=str(body.person_request_id),
                display_name=body.display_name,
            )
            person = created["resource_id"]
        else:
            person = str(body.person_id)
            await read(client, f"persons/{person}")
        if body.owner_request_id is not None:
            await identity_mutation(
                client,
                f"character/owners/{person}/grant",
                request_id=str(body.owner_request_id),
            )
        issued = await identity_mutation(
            client,
            f"persons/{person}/client-pair",
            request_id=str(body.pair_request_id),
            purpose="chat",
        )
        return {"person_id": person, **issued}


def read_input() -> str:
    return sys.stdin.read()


def write_result(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False))
