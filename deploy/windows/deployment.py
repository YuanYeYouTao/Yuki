"""Private Windows/WSL deployment configuration and bounded readiness probes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import tarfile
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

ROUTES = (
    "chat_agent",
    "memory_extraction",
    "memory_self_reflection",
    "memory_consolidation",
    "memory_dream",
    "memory_attribution",
    "relationship_evaluation",
    "emoji_replacement",
    "automation_text_generation",
    "automation_agent",
    "plugin_agent_session",
    "utility_structured",
    "conversation_compaction",
)


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(text)


def validate_credentials(key: str, model: str, base: str) -> dict[str, str]:
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("A valid HTTPS Anthropic endpoint is required")
    if parsed.username or parsed.password:
        raise ValueError("Endpoint must not contain credentials")
    if any(char.isspace() or char in {'"', "'", "\\", "$"} for char in base):
        raise ValueError("Invalid endpoint")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{8,4096}", key):
        raise ValueError("Invalid API credential")
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", model):
        raise ValueError("Invalid model identifier")
    if not parsed.path or parsed.path == "/":
        base += "/v1"
    return {"base_url": base, "api_key": key, "model": model}


def read_credentials(path: Path) -> dict[str, str]:
    # Markdown is input data, never executable shell or Python.
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        cells = [cell.strip().strip("`").strip() for cell in line.split("|")]
        if len(cells) >= 4:
            fields[cells[1].casefold()] = cells[2]
    key, model = fields.get("api_key", ""), fields.get("model", "")
    base = fields.get("base_url (anthropic)", "").rstrip("/")
    # The supplied gateway names its Anthropic host; Yuki expects its API prefix.
    return validate_credentials(key, model, base)


def profiles_text() -> str:
    profiles = ["schema_version = 3\n"]
    for name, effort, tokens in (("main", "high", 16384), ("background", "low", 8192)):
        profiles.append(f'''\n[profiles.{name}]
provider = "anthropic"
protocol = "anthropic_messages"
base_url_env = "LLM_BASE_URL"
api_key_env = "LLM_API_KEY"
model_env = "LLM_MODEL"
timeout_seconds = 180.0
max_retries = 1
default_temperature = 0.7
default_max_output_tokens = {tokens}
thinking_mode = "enabled"
reasoning_effort = "{effort}"
structured_output_mode = "function_tool"
capabilities = ["tools", "structured_output", "reasoning", "long_context", "image_input"]

[profiles.{name}.wire_options]
reasoning = "budget"
thinking_budget_tokens = 1024
''')
    profiles.append("\n[routes]\n")
    for route in ROUTES:
        profile = (
            "main"
            if route in {"chat_agent", "automation_agent", "plugin_agent_session"}
            else "background"
        )
        profiles.append(f'{route} = "{profile}"\n')
    return "".join(profiles)


def configure(
    bundle: Path,
    app: Path,
    admin: str,
    artifacts_path: Path = Path("/opt/yuki-monty/artifacts.json"),
) -> None:
    bot_qq = str(json.loads((bundle / "manifest.json").read_text())["bot_qq"])
    if not re.fullmatch(r"[1-9][0-9]{4,19}", bot_qq):
        raise ValueError("Invalid Bot QQ in the private manifest")
    if not re.fullmatch(r"[1-9][0-9]{4,19}", admin) or admin == bot_qq:
        raise ValueError("Enter the human administrator QQ, distinct from the Bot QQ")
    config = app / "webui-config"
    if (config / "runtime.env").exists():
        link_configuration(app)
        print("Existing private configuration preserved.")
        return
    if (app / ".env").exists():
        print("Existing private configuration preserved.")
        return
    if config.exists():
        raise ValueError("Unowned webui-config content exists; no configuration was overwritten")
    credentials = json.loads((bundle / "private/provider.json").read_text())
    # Revalidate the JSON too, without writing it into a public report.
    if set(credentials) != {"base_url", "api_key", "model"}:
        raise ValueError("Invalid private provider document")
    if any(
        not isinstance(value, str) or "\n" in value or "\r" in value or '"' in value
        for value in credentials.values()
    ):
        raise ValueError("Invalid private provider value")
    credentials = validate_credentials(
        credentials["api_key"], credentials["model"], credentials["base_url"]
    )
    artifacts = json.loads(artifacts_path.read_text())
    for name in ("worker", "launcher"):
        if not re.fullmatch(r"[0-9a-f]{64}", artifacts[name]["sha256"]):
            raise ValueError("Invalid installed Monty identity")
    onebot, webui, napcat = (secrets.token_hex(32) for _ in range(3))
    prompt = (bundle / "private/persona.md").read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("The supplied persona is empty")
    # The server's existing reviewed catalog determines grants; no wildcard or SQL access.
    from qq_ai_bot.control_plane.capabilities import CONTROL_CAPABILITY_IDS

    operator = {
        "principal_id": str(uuid.uuid4()),
        "token_env": "YUKI_CONTROL_OPERATOR_TOKEN",
        "enabled": True,
        "roles": ["operator"],
        "capabilities": sorted(CONTROL_CAPABILITY_IDS),
    }
    toml = (
        "[[operators]]\n"
        + "\n".join(
            f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in operator.items()
        )
        + "\n"
    )
    settings = {
        "APP_HOST": "0.0.0.0",
        "APP_PORT": "18765",
        "DATABASE_URL": "sqlite+aiosqlite:///./data/qq_ai_bot.db",
        "ONEBOT_ACCESS_TOKEN": onebot,
        "SUPERUSERS": admin,
        "LLM_PROVIDER": "anthropic",
        "LLM_BASE_URL": credentials["base_url"],
        "LLM_API_KEY": credentials["api_key"],
        "LLM_MODEL": credentials["model"],
        "LLM_MAX_OUTPUT_TOKENS": "16384",
        "LLM_REASONING_EFFORT": "high",
        "MODEL_PROFILES_FILE": "webui-config/model_profiles.toml",
        "SYSTEM_PROMPT_FILE": "webui-config/system_prompt.md",
        "BOT_PERSONA_FILE": "webui-config/persona.md",
        "BOT_DISPLAY_NAME": "波奇",
        "BOT_ALIASES": "波奇,Bocchi,bocchi,后藤一里,ぼっち",
        "BOT_VOICE_NAME": "ぼっち",
        "WEBUI_ENABLED": "true",
        "WEBUI_ORIGIN": "http://127.0.0.1:18765",
        "CONTROL_OPERATORS_FILE": "webui-config/control-operators.toml",
        "YUKI_CONTROL_OPERATOR_TOKEN": webui,
        "SOCIAL_TRANSFER_DIRECTORY": "social-transfer",
        "SOCIAL_GATEWAY_TRANSFER_DIRECTORY": "/yuki-transfer",
        "CODE_MODE_WORKER_PATH": "/opt/yuki-monty/monty",
        "CODE_MODE_WORKER_SHA256": artifacts["worker"]["sha256"],
        "CODE_MODE_LAUNCHER_PATH": "/opt/yuki-monty/monty-isolated",
        "CODE_MODE_LAUNCHER_SHA256": artifacts["launcher"]["sha256"],
        "RUNTIME_WORK_ENABLED": "true",
        "AUTOMATION_ENABLED": "true",
        "SUBAGENTS_ENABLED": "true",
        "WEB_MODE": "disabled",
        "VISION_ENABLED": "false",
        "ASR_ENABLED": "false",
        "SPEECH_ENABLED": "false",
        "LOG_MESSAGE_CONTENT": "false",
        "DEFAULT_TIMEZONE": "Asia/Shanghai",
    }
    # EnvironmentFile and Settings .env use the same simple quoted assignments.
    files = {
        "persona.md": prompt,
        "system_prompt.md": "{{YUKI_PERSONA_CORE}}\n",
        "model_profiles.toml": profiles_text(),
        "control-operators.toml": toml,
        "runtime.env": "\n".join(f'{key}="{value}"' for key, value in settings.items()) + "\n",
        "gateway.env": f"NAPCAT_ACCOUNT={bot_qq}\nNAPCAT_WEBUI_TOKEN={napcat}\n",
        "management.txt": (
            f"Yuki WebUI: http://127.0.0.1:18765\nCredential: {webui}\n"
            f"NapCat: http://127.0.0.1:6099\nCredential: {napcat}\n"
            f"Bot QQ: {bot_qq}\nAdministrator QQ: {admin}\n"
        ),
    }
    app.mkdir(parents=True, exist_ok=True)
    # Publish all credentials and routes together. A failed staging write cannot
    # leave a half-configured .env that prevents an ordinary retry.
    with tempfile.TemporaryDirectory(prefix=".yuki-config-", dir=app) as temporary:
        staged = Path(temporary) / "ready"
        staged.mkdir(mode=0o700)
        for name, content in files.items():
            private_write(staged / name, content)
        staged.rename(config)
    link_configuration(app)
    print("Bocchi, provider, original Work runtime and native Code Mode configured.")


def link_configuration(app: Path) -> None:
    for name, target in {
        ".env": "webui-config/runtime.env",
        "management.txt": "webui-config/management.txt",
        "gateway/.env": "../webui-config/gateway.env",
    }.items():
        path = app / name
        if path.exists() or path.is_symlink():
            continue  # Preserve operator replacements too.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)


def verify_bundle(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        target = (bundle / name).resolve()
        if not target.is_relative_to(bundle.resolve()) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid bundle manifest path or digest")
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("Bundle checksum mismatch: " + name)
    return manifest


def extract_source(bundle: Path, app: Path) -> None:
    manifest = verify_bundle(bundle)
    marker = app / ".yuki-source-revision"
    if marker.exists():
        if marker.read_text().strip() != manifest["source_revision"]:
            raise ValueError(
                "Existing installation has a different revision; automatic upgrade refused"
            )
        return
    if app.exists() and any(app.iterdir()):
        raise ValueError("Existing application content has no deployment ownership marker")
    app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yuki-source-", dir=app.parent) as temporary:
        staged = Path(temporary) / "ready"
        staged.mkdir(mode=0o700)
        with tarfile.open(bundle / "source.tar.gz") as archive:
            archive.extractall(staged, filter="data")
        private_write(staged / marker.name, manifest["source_revision"] + "\n")
        staged.rename(app)


async def api_probe(app: Path) -> dict:
    import base64
    import io

    from PIL import Image

    from qq_ai_bot.config import Settings
    from qq_ai_bot.domain.messages import (
        ChatImage,
        ChatMessage,
        ChatRequest,
        ChatTool,
        FunctionCallOutput,
        ModelResponseStatus,
    )
    from qq_ai_bot.model_runtime.pool import ModelClientPool
    from qq_ai_bot.model_runtime.profiles import (
        load_model_profile_catalog,
        model_profile_environment,
    )

    settings = Settings(_env_file=app / ".env")
    catalog = load_model_profile_catalog(
        settings.model_profiles_file,
        legacy_provider=settings.llm_provider,
        legacy_base_url=settings.llm_base_url,
        legacy_model=settings.llm_model,
        legacy_timeout_seconds=settings.llm_timeout_seconds,
        legacy_max_retries=0,
        legacy_temperature=settings.llm_temperature,
        legacy_max_output_tokens=settings.llm_max_output_tokens,
        legacy_thinking_enabled=True,
        environment=model_profile_environment(settings),
    )
    pool = ModelClientPool(secret_overrides={"LLM_API_KEY": settings.llm_api_key})
    profile = catalog.profiles["main"]
    pixels = io.BytesIO()
    Image.new("RGB", (64, 48), "blue").save(pixels, format="PNG")
    image = ChatImage("data:image/png;base64," + base64.b64encode(pixels.getvalue()).decode())
    request = ChatRequest(
        messages=(
            ChatMessage(
                "system",
                "This is a deployment connectivity probe. "
                "Read the attached image and call emit_result exactly once with ok=true "
                "and dominant_color chosen from the tool schema. "
                "After its result, reply with only READY and call no tools.",
            ),
            ChatMessage("user", "Check this image.", images=(image,)),
        ),
        model=profile.model,
        max_output_tokens=profile.default_max_output_tokens,
        thinking_enabled=True,
        reasoning_effort=profile.reasoning_effort,
        tools=(
            ChatTool(
                "emit_result",
                "Return the probe result",
                {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "dominant_color": {
                            "type": "string",
                            "enum": ["blue", "red", "green", "other"],
                        },
                    },
                    "required": ["ok", "dominant_color"],
                    "additionalProperties": False,
                },
            ),
        ),
    )
    started = time.monotonic()
    try:
        provider = pool.get(profile.model_copy(update={"max_retries": 0}))
        first = await provider.complete(request)
        if (
            first.status != ModelResponseStatus.COMPLETED
            or len(first.tool_calls) != 1
            or first.continuation is None
        ):
            raise ValueError("Provider did not return one complete tool call with continuation")
        call = first.tool_calls[0]
        if call.function.name != "emit_result" or json.loads(call.function.arguments) != {
            "ok": True,
            "dominant_color": "blue",
        }:
            raise ValueError("Provider returned an invalid structured probe result")
        second = await provider.complete(
            replace(
                request,
                continuation=first.continuation,
                continuation_items=(FunctionCallOutput(call.id, call.function.arguments),),
            )
        )
        if (
            second.status != ModelResponseStatus.COMPLETED
            or second.tool_calls
            or second.content.strip() != "READY"
        ):
            raise ValueError("Provider tool continuation did not complete")
        return {
            "verdict": "passed",
            "protocol": profile.protocol.value,
            "model": profile.model,
            "requests": 2,
            "image_input": True,
            "observed_color": "blue",
            "seconds": round(time.monotonic() - started, 3),
            "reported_input_tokens": [first.prompt_tokens, second.prompt_tokens],
            "reported_output_tokens": [first.completion_tokens, second.completion_tokens],
        }
    finally:
        await pool.close()


def verify_database(app: Path) -> None:
    with sqlite3.connect(f"file:{app / 'data/qq_ai_bot.db'}?mode=ro", uri=True) as connection:
        if connection.execute("SELECT version_num FROM alembic_version").fetchall() != [("0096",)]:
            raise ValueError("Database did not reach the packaged 0096 head")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity probe failed")
    print("Database head 0096 and quick_check passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("extract", "configure", "api-probe", "database"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--app", type=Path, default=Path.cwd())
    parser.add_argument("--admin", default="")
    args = parser.parse_args()
    try:
        if args.action == "extract":
            extract_source(args.bundle, args.app)
        elif args.action == "configure":
            configure(args.bundle, args.app, args.admin)
        elif args.action == "database":
            verify_database(args.app)
        else:
            result = asyncio.run(api_probe(args.app))
            (args.app / "deployment-evidence/api-probe.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            print(json.dumps(result))
    except Exception as exc:
        # Upstream exception text may contain a key or request. Emit only its class.
        print(
            f"Deployment {args.action} failed ({type(exc).__name__}); credentials are hidden.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
