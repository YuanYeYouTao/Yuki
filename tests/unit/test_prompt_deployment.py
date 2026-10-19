"""Actual canonical copies, source-preserving env edits and interrupted CLI recovery."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from yuki.config import read_prompt_documents
from yuki.deployment_setup.prompts import (
    prepare_character_files,
    prepare_prompt_deployment,
    source_path,
)
from yuki.deployment_setup.service import EnvironmentDocument, SetupPaths, SetupValidationError


def deployment(tmp_path, *, system_file=False, legacy=False):
    paths = SetupPaths(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    persona = config / "custom-persona.md"
    persona.write_bytes(" 私人人格\r\n第二行 ".encode())
    prompt = "HEADER\n{{YUKI_PERSONA_CORE}}"
    text = 'UNRELATED="keep\nthese lines" # preserved\n# operator note\n'
    if system_file:
        source = config / "custom-system.md"
        source.write_bytes(("  " + prompt.replace("\n", "\r\n") + "\r\n ").encode())
        text += "SYSTEM_PROMPT_FILE=/app/config/custom-system.md\n"
    else:
        text += 'SYSTEM_PROMPT="' + prompt + '"\n'
    text += ("YUKI_PERSONA_FILE" if legacy else "BOT_PERSONA_FILE") + "=config/custom-persona.md\n"
    paths.env.write_text(text, encoding="utf-8")
    return paths, prompt


@pytest.mark.parametrize("system_file,legacy", [(False, False), (True, False), (True, True)])
def test_actual_sources_copy_unchanged_and_unrelated_multiline_env_survives(
    tmp_path, system_file, legacy
):
    paths, prompt = deployment(tmp_path, system_file=system_file, legacy=legacy)
    before = EnvironmentDocument.load(paths).prompt_source_values()
    source_system = source_path(paths, before["SYSTEM_PROMPT_FILE"]) if system_file else None
    source_persona = source_path(
        paths, before.get("BOT_PERSONA_FILE") or before["YUKI_PERSONA_FILE"]
    )
    original = read_prompt_documents(source_system, source_persona, system_prompt_template=prompt)
    result = prepare_prompt_deployment(paths)
    assert result["restart_required"]
    current = dotenv_values(paths.env)
    assert current["SYSTEM_PROMPT_FILE"] == "/app/character/system_prompt.md"
    assert current["BOT_PERSONA_FILE"] == "/app/character/persona.md"
    assert "SYSTEM_PROMPT" not in current and "YUKI_PERSONA_FILE" not in current
    assert current["UNRELATED"] == "keep\nthese lines"
    assert 'UNRELATED="keep\nthese lines" # preserved' in paths.env.read_text()
    assert "# operator note" in paths.env.read_text()
    system, persona = tmp_path / "character/system_prompt.md", tmp_path / "character/persona.md"
    copied = read_prompt_documents(system, persona, system_prompt_template="unused")
    assert copied.system_prompt == original.system_prompt and copied.persona == original.persona
    assert persona.read_bytes() == source_persona.read_bytes()
    assert system.read_bytes() == (source_system.read_bytes() if source_system else prompt.encode())
    if os.name != "nt":
        assert stat.S_IMODE(persona.stat().st_mode) == 0o640
    inodes = system.stat().st_ino, persona.stat().st_ino
    assert not prepare_prompt_deployment(paths)["restart_required"]
    assert (system.stat().st_ino, persona.stat().st_ino) == inodes
    assert not (tmp_path / ".yuki/backups").exists()
    assert "HEADER" not in json.dumps(result)


def test_existing_character_content_is_preserved_and_env_does_not_change(tmp_path):
    paths, _ = deployment(tmp_path)
    character = tmp_path / "character"
    character.mkdir()
    (character / "persona.md").write_text("Existing operator work")
    before = paths.env.read_bytes()
    with pytest.raises(SetupValidationError, match="内容不同"):
        prepare_prompt_deployment(paths)
    assert paths.env.read_bytes() == before
    assert (character / "persona.md").read_text() == "Existing operator work"
    assert not (character / "system_prompt.md").exists()


def test_empty_or_changed_sources_are_not_published(tmp_path):
    paths, _ = deployment(tmp_path)
    (tmp_path / "config/custom-persona.md").write_text(" \n")
    before = paths.env.read_bytes()
    with pytest.raises(SetupValidationError, match="无法读取"):
        prepare_prompt_deployment(paths)
    assert paths.env.read_bytes() == before and not (tmp_path / "character").exists()


def test_env_boundary_whitespace_cannot_be_silently_changed_by_file_rules(tmp_path):
    paths, _ = deployment(tmp_path)
    values = EnvironmentDocument.load(paths).prompt_source_values()
    values["SYSTEM_PROMPT"] = " prefix {{YUKI_PERSONA_CORE}} "
    with pytest.raises(SetupValidationError, match="不能无损迁移"):
        prepare_character_files(paths, values)
    assert not (tmp_path / "character").exists()


def test_legacy_default_persona_alias_uses_existing_runtime_fallback(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text("legacy persona")
    result = prepare_character_files(
        SetupPaths(tmp_path),
        {
            "YUKI_PERSONA_FILE": "config/yuki_persona_core.md",
            "SYSTEM_PROMPT": "{{YUKI_PERSONA_CORE}}",
        },
    )
    assert result["revisions"]["persona"].startswith("sha256:")
    assert (tmp_path / "character/persona.md").read_text() == "legacy persona"


def test_container_paths_resolve_inside_selected_deployment_root(tmp_path):
    assert (
        source_path(SetupPaths(tmp_path), "/app/character/persona.md")
        == tmp_path / "character/persona.md"
    )
    assert source_path(SetupPaths(tmp_path), "config/persona.md") == tmp_path / "config/persona.md"


async def test_real_cli_crash_after_first_publication_then_retries_same_files(tmp_path):
    paths, _ = deployment(tmp_path)
    before = paths.env.read_bytes()
    child = r"""
import os, sys
from pathlib import Path
from yuki.cli import main
import yuki.deployment_setup.prompts as module
publish = module._publish

def interrupted(*args):
    publish(*args)
    os._exit(73)
module._publish = interrupted
sys.argv = ["yuki", "setup", "prepare-prompts", "--deployment-root", sys.argv[1], "--no-color"]
main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _output, errors = await process.communicate()
    assert process.returncode == 73, errors.decode()
    first = tmp_path / "character/system_prompt.md"
    assert first.is_file() and not (tmp_path / "character/persona.md").exists()
    inode = first.stat().st_ino
    assert paths.env.read_bytes() == before
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "yuki.cli",
        "setup",
        "prepare-prompts",
        "--deployment-root",
        str(tmp_path),
        "--no-color",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await process.communicate()
    assert process.returncode == 0, errors.decode()
    result = json.loads(output)
    assert result["restart_required"] and first.stat().st_ino == inode
    assert (tmp_path / "character/persona.md").is_file()


def test_process_source_override_is_explicit_and_uses_actual_value(tmp_path, monkeypatch):
    paths, _ = deployment(tmp_path)
    monkeypatch.setenv("SYSTEM_PROMPT", "PROCESS {{YUKI_PERSONA_CORE}}")
    original = EnvironmentDocument.load(paths)
    assert original.prompt_source_values()["SYSTEM_PROMPT"].startswith("HEADER")
    assert original.prompt_source_values(include_process=True)["SYSTEM_PROMPT"].startswith(
        "PROCESS"
    )
    result = prepare_prompt_deployment(paths, include_process=True)
    assert result["restart_required"]
    assert (tmp_path / "character/system_prompt.md").read_text() == "PROCESS {{YUKI_PERSONA_CORE}}"


def test_validation_honors_current_custom_persona_and_migrated_paths(tmp_path):
    from tests.unit.test_deployment_setup import _search_setup

    from yuki.deployment_setup.service import build_model_profiles, validate_configuration

    profiles = build_model_profiles(main_protocol="gemini", flash_enabled=False)
    paths, configuration = _search_setup(
        tmp_path,
        profiles,
        WEB_MODE="disabled",
        BOT_PERSONA_FILE="/app/character/persona.md",
        SYSTEM_PROMPT_FILE="/app/character/system_prompt.md",
    )
    (tmp_path / "character").mkdir()
    (tmp_path / "character/persona.md").write_text("CURRENT_PERSONA")
    (tmp_path / "character/system_prompt.md").write_text("SYSTEM {{YUKI_PERSONA_CORE}}")
    settings = validate_configuration(paths, configuration)
    assert (
        settings.bot_persona == "CURRENT_PERSONA"
        and settings.system_prompt == "SYSTEM CURRENT_PERSONA"
    )


def test_private_runtime_files_are_absent_from_actual_release_archives(tmp_path):
    import shutil
    import tarfile
    import zipfile

    from scripts.build_release_bundle import (
        build_release_bundle,
        select_bundle_files,
        tracked_files,
    )
    from scripts.release_validate import project_version

    root = Path(__file__).parents[2]
    version = project_version(root)
    tracked = tracked_files(root) | {"docs/operations/character-prompt-files.md"}
    selected = select_bundle_files(tracked, version)
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
    for relative in selected:
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)
    private = source / "character/persona.md"
    private.parent.mkdir()
    private.write_bytes(b"PRIVATE_RUNTIME_SENTINEL")
    tracked.add("character/persona.md")
    output = tmp_path / "bundles"
    build_release_bundle(source, output, version, tracked=tracked)
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert not any(name.endswith("/character/persona.md") for name in archive.namelist())
        assert not any(
            b"PRIVATE_RUNTIME_SENTINEL" in archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
        )
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        assert not any(
            member.name.endswith("/character/persona.md") for member in archive.getmembers()
        )


def test_concurrent_env_edit_is_not_overwritten(tmp_path, monkeypatch):
    import yuki.deployment_setup.prompts as module

    paths, _ = deployment(tmp_path)
    prepare = module.prepare_character_files
    latest = paths.env.read_text() + "NEW_OPERATOR_VALUE=keep\n"

    def edited(*args):
        result = prepare(*args)
        paths.env.write_text(latest)
        return result

    monkeypatch.setattr(module, "prepare_character_files", edited)
    with pytest.raises(SetupValidationError, match="未覆盖"):
        module.prepare_prompt_deployment(paths)
    assert paths.env.read_text() == latest
    assert not paths.restart_required.exists()


def test_concurrent_source_edit_is_retained_without_activating_stale_copy(tmp_path, monkeypatch):
    import yuki.deployment_setup.prompts as module

    paths, _ = deployment(tmp_path)
    before = paths.env.read_bytes()
    publish = module._publish

    def edited(*args):
        publish(*args)
        (tmp_path / "config/custom-persona.md").write_text("LATER_OPERATOR_PERSONA")

    monkeypatch.setattr(module, "_publish", edited)
    with pytest.raises(SetupValidationError, match="来源在准备期间改变"):
        module.prepare_prompt_deployment(paths)
    assert paths.env.read_bytes() == before
    assert (tmp_path / "config/custom-persona.md").read_text() == "LATER_OPERATOR_PERSONA"


def test_direct_preparation_preserves_unrelated_crlf_bytes(tmp_path):
    paths, _ = deployment(tmp_path)
    original = paths.env.read_bytes().replace(b"\n", b"\r\n")
    paths.env.write_bytes(original)
    unrelated = b'UNRELATED="keep\r\nthese lines" # preserved\r\n# operator note\r\n'
    assert unrelated in original
    prepare_prompt_deployment(paths)
    assert unrelated in paths.env.read_bytes()
    assert b'SYSTEM_PROMPT="HEADER' not in paths.env.read_bytes()
