"""The renamed distribution still produces runnable, source-free release assets."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.build_release_bundle import (
    BundleBuildError,
    build_release_bundle,
    select_bundle_files,
    tracked_files,
)
from scripts.release_validate import project_version, validate_release_identity

ROOT = Path(__file__).resolve().parents[2]


def test_release_identity_reads_the_renamed_editable_distribution():
    version = project_version(ROOT)
    assert validate_release_identity(ROOT, f"v{version}") == version


def test_source_free_release_archives_match_the_loose_assets(tmp_path):
    version = project_version(ROOT)
    assets = build_release_bundle(ROOT, tmp_path, version)
    archive_root = f"yuki-{version}-deploy"
    compose = (tmp_path / "docker-compose.yml").read_bytes()
    assert b"name: yuki\n" in compose
    assert b"image: ghcr.io/yuanyeyoutao/yuki:" in compose
    assert b"yuki-network" in compose
    for name in ("install.sh", "install.ps1"):
        installer = (tmp_path / name).read_bytes()
        assert b"YuanYeYouTao/Yuki" in installer
        assert b"ghcr.io/yuanyeyoutao/yuki" in installer
        assert b"--entrypoint yuki" in installer
        assert b"qq-ai-bot" not in installer
    with zipfile.ZipFile(tmp_path / f"{archive_root}.zip") as archive:
        assert archive.read(f"{archive_root}/docker-compose.yml") == compose
        assert not any(name.startswith(f"{archive_root}/src/") for name in archive.namelist())
        assert f"{archive_root}/.env" not in archive.namelist()
    with tarfile.open(tmp_path / f"{archive_root}.tar.gz", "r:gz") as archive:
        member = archive.extractfile(f"{archive_root}/docker-compose.yml")
        assert member is not None and member.read() == compose
    assert {asset.name for asset in assets} >= {"SHA256SUMS", "install.sh", "install.ps1"}


@pytest.mark.parametrize("filename", ["qq_ai_bot.db", "yuki.db"])
def test_release_bundle_rejects_both_real_ledger_filenames(filename):
    files = tracked_files(ROOT) | {f"plugins/unintended-data/{filename}"}
    with pytest.raises(BundleBuildError, match="forbidden bundle path"):
        select_bundle_files(files, project_version(ROOT))
