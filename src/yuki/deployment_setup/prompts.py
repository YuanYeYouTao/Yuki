"""One-time, content-preserving preparation of the canonical writable prompt pair."""

import hashlib
import os
import tempfile
from collections.abc import Mapping
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any

from dotenv.parser import parse_stream

from yuki.config import Settings, prompt_document_revision, read_prompt_documents
from yuki.deployment_setup.service import (
    EnvironmentDocument,
    SetupPaths,
    SetupValidationError,
    _atomic_write,
)

RUNTIME_PATHS = {
    "SYSTEM_PROMPT_FILE": "/app/character/system_prompt.md",
    "BOT_PERSONA_FILE": "/app/character/persona.md",
}


def source_path(paths: SetupPaths, value: str) -> Path:
    container = PurePosixPath(value)
    if container.is_absolute() and container.is_relative_to("/app"):
        return paths.root.joinpath(*container.relative_to("/app").parts)
    path = Path(value)
    return path if path.is_absolute() else paths.root / path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(path: Path, content: bytes) -> None:
    """Publish a complete new file without replacing an existing operator file."""
    descriptor, name = tempfile.mkstemp(prefix=".yuki-prompt-prepare-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o640)
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            with path.open("rb") as existing:
                matches = existing.read() == content
                os.fsync(existing.fileno())
            if not matches:
                raise SetupValidationError(
                    f"character/{path.name} 已存在且内容不同，未覆盖"
                ) from None
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_character_files(paths: SetupPaths, environment: Mapping[str, str]) -> dict[str, Any]:
    system_value = environment.get("SYSTEM_PROMPT_FILE", "").strip()
    system = source_path(paths, system_value) if system_value else None
    bot_persona = environment.get("BOT_PERSONA_FILE", "").strip()
    legacy = environment.get("YUKI_PERSONA_FILE", "").strip()
    persona = source_path(paths, bot_persona or legacy or "config/persona.md")
    if not bot_persona and legacy == "config/yuki_persona_core.md" and not persona.exists():
        persona = paths.root / "config/persona.md"
    template = environment.get("SYSTEM_PROMPT", str(Settings.model_fields["system_prompt"].default))
    try:
        original = read_prompt_documents(system, persona, system_prompt_template=template)
        system_bytes = system.read_bytes() if system else template.encode("utf-8")
        persona_bytes = persona.read_bytes()
    except (OSError, ValueError) as exc:
        raise SetupValidationError("无法读取当前实际提示词来源，未修改配置") from exc
    if (
        prompt_document_revision(system_bytes) != original.system_revision
        or prompt_document_revision(persona_bytes) != original.persona_revision
    ):
        raise SetupValidationError("提示词来源在准备期间改变，请重新核对")
    if system is None and template.replace("\r\n", "\n").replace("\r", "\n").strip() != template:
        raise SetupValidationError(
            "SYSTEM_PROMPT 的边界空白或换行与文件加载规则不同，不能无损迁移；未修改配置"
        )
    destination = paths.root / "character"
    documents = {
        destination / "system_prompt.md": system_bytes,
        destination / "persona.md": persona_bytes,
    }
    # Inspect both existing destinations before publishing either one.
    for path, content in documents.items():
        if path.exists() and path.read_bytes() != content:
            raise SetupValidationError(f"character/{path.name} 已存在且内容不同，未覆盖")
    if not destination.exists():
        destination.mkdir(mode=0o750)
        _sync_directory(paths.root)
    for path, content in documents.items():
        _publish(path, content)
    copied = read_prompt_documents(
        destination / "system_prompt.md",
        destination / "persona.md",
        system_prompt_template=template,
    )
    if copied.system_prompt != original.system_prompt or copied.persona != original.persona:
        raise SetupValidationError("复制后渲染结果不同，未修改环境变量")
    latest = read_prompt_documents(system, persona, system_prompt_template=template)
    if latest.revisions != original.revisions:
        raise SetupValidationError("提示词来源在准备期间改变，未修改环境变量")
    return {
        "environment": dict(RUNTIME_PATHS),
        "revisions": copied.revisions,
        "rendered_sha256": hashlib.sha256(copied.system_prompt.encode("utf-8")).hexdigest(),
        "restart_required": True,
    }


def _updated_source_environment(text: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    output: list[str] = []
    written: set[str] = set()
    for binding in parse_stream(StringIO(text)):
        key = binding.key.upper() if binding.key else None
        if key in {"SYSTEM_PROMPT", "YUKI_PERSONA_FILE"}:
            continue
        if key in RUNTIME_PATHS:
            if key not in written:
                output.append(f"{key}={RUNTIME_PATHS[key]}{newline}")
                written.add(key)
        else:
            output.append(binding.original.string)
    result = "".join(output)
    for key, value in RUNTIME_PATHS.items():
        if key in written:
            continue
        if result and not result.endswith(("\r", "\n")):
            result += newline
        result += f"{key}={value}{newline}"
    return result


def prepare_prompt_deployment(
    paths: SetupPaths, *, include_process: bool = False
) -> dict[str, Any]:
    source = paths.env if paths.env.is_file() else paths.env_example
    original = source.read_bytes()
    env_before = paths.env.read_bytes() if paths.env.is_file() else None
    document = EnvironmentDocument(original.decode("utf-8"))
    result = prepare_character_files(
        paths, document.prompt_source_values(include_process=include_process)
    )
    # Only these source selectors change; unrelated settings and multiline values survive.
    updated = _updated_source_environment(original.decode("utf-8"))
    encoded = updated.encode("utf-8")
    current_env = paths.env.read_bytes() if paths.env.is_file() else None
    if source.read_bytes() != original or current_env != env_before:
        raise SetupValidationError("环境配置在准备期间改变，未覆盖新内容")
    changed = not paths.env.is_file() or paths.env.read_bytes() != encoded
    if changed:
        _atomic_write(paths.env, encoded, private=True)
        _sync_directory(paths.root)
        paths.restart_required.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(paths.restart_required, b"prompt-paths-changed\n", private=False)
    result["restart_required"] = changed
    return result
