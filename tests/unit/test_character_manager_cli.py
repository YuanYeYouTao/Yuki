"""Real Manager construction through its CLI, without Docker or shared HOME."""

from __future__ import annotations

import sys

from yuki.sandbox import manager, persistent


async def test_manager_cli_keeps_each_character_environment_and_home(tmp_path, monkeypatch, capsys):
    created = []
    original = persistent.PersistentManager

    def construct(*args, **kwargs):
        instance = original(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(persistent, "PersistentManager", construct)
    try:
        for slug in ("yuki", "mika"):
            root = tmp_path / slug
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "manager",
                    "--root",
                    str(root / "manager"),
                    "--workspace",
                    str(root / "artifacts"),
                    "--socket",
                    str(root / "manager.sock"),
                    "--persistent-home",
                    str(root / "home"),
                    "--image",
                    "fixture-image",
                    "--name",
                    f"yuki-environment-{slug}",
                    "--migrate-only",
                ],
            )
            await manager.main()
        assert [item.name for item in created] == ["yuki-environment-yuki", "yuki-environment-mika"]
        assert created[0].root != created[1].root
        assert created[0].home != created[1].home
        (created[0].home / "workspace" / "private.txt").write_text("first role only")
        assert not (created[1].home / "workspace" / "private.txt").exists()
        assert len(capsys.readouterr().out.splitlines()) == 2
    finally:
        for instance in created:
            await instance.close()


async def test_manager_cli_preserves_the_existing_default_name(tmp_path, monkeypatch):
    created = []
    original = persistent.PersistentManager

    def construct(*args, **kwargs):
        instance = original(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(persistent, "PersistentManager", construct)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "manager",
            "--root",
            str(tmp_path / "manager"),
            "--workspace",
            str(tmp_path / "artifacts"),
            "--socket",
            str(tmp_path / "manager.sock"),
            "--persistent-home",
            str(tmp_path / "home"),
            "--image",
            "fixture-image",
            "--migrate-only",
        ],
    )
    try:
        await manager.main()
        assert created[0].name == "yuki-environment"
    finally:
        for instance in created:
            await instance.close()
