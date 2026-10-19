"""Exercise actual operator-file ownership and sequential procedure processes."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def test_generated_operator_file_preserves_user_edits(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "deploy/characters"))
    product = importlib.import_module("configure_collaboration")
    path, receipts = tmp_path / "relay.conf", tmp_path / "receipts.json"
    product.owned_file(path, b"first generation", receipts)
    product.owned_file(path, b"second generation", receipts)
    assert path.read_bytes() == b"second generation"
    path.write_bytes(b"operator edit")
    with pytest.raises(ValueError, match="modified operator file"):
        product.owned_file(path, b"third generation", receipts)
    assert path.read_bytes() == b"operator edit"
    unknown = tmp_path / "original.conf"
    unknown.write_bytes(b"preexisting configuration")
    with pytest.raises(ValueError, match="modified operator file"):
        product.owned_file(unknown, b"replace original", receipts)
    link = tmp_path / "link.conf"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="operator symlink"):
        product.owned_file(link, b"new", receipts)
    assert path.read_bytes() == b"operator edit"


def test_fleet_runs_original_compose_context_and_stops_on_actual_failure(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "deploy/characters"))
    registry, records = tmp_path / "registry", tmp_path / "records.jsonl"
    registry.mkdir()
    for slug in ("mika", "other", "yuki"):
        root = registry / slug
        root.mkdir()
        (root / "instance.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "project_root": str(root),
                    "compose_project": "original-" + slug,
                    "compose_files": [str(root / "compose.yaml"), str(root / "gateways.yaml")],
                    "env_file": str(root / "original.env"),
                }
            )
        )
    procedure = tmp_path / "procedure"
    procedure.write_text(
        "#!" + os.sys.executable + "\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "v={key:os.environ[key] for key in ('COMPOSE_PROJECT_NAME','COMPOSE_FILE',"
        "'COMPOSE_ENV_FILES','YUKI_INSTANCE_OPERATION','YUKI_INSTANCE_SLUG')}\n"
        "v.update(cwd=os.getcwd(),argv=sys.argv[1:])\n"
        "with Path(" + repr(str(records)) + ").open('a') as f: f.write(json.dumps(v)+'\\n')\n"
        "sys.exit(17 if v['YUKI_INSTANCE_SLUG']=='other' else 0)\n"
    )
    procedure.chmod(0o700)
    result = subprocess.run(
        [
            str(ROOT / "deploy/characters/backup-all.sh"),
            "--registry",
            str(registry),
            "--procedure",
            str(procedure),
            "--",
            "--verify",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "exit 17" in result.stderr
    values = [json.loads(line) for line in records.read_text().splitlines()]
    assert [value["YUKI_INSTANCE_SLUG"] for value in values] == ["mika", "other"]
    assert all(value["YUKI_INSTANCE_OPERATION"] == "backup" for value in values)
    assert values[0]["COMPOSE_PROJECT_NAME"] == "original-mika"
    assert values[0]["COMPOSE_ENV_FILES"].endswith("mika/original.env")
    assert values[0]["COMPOSE_FILE"].endswith("gateways.yaml")
    assert values[0]["argv"] == ["--verify"]
    assert values[0]["cwd"] == str(registry / "mika")
