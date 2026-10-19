"""Existing files must not be hidden by a newly mounted role filesystem."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("location", ["home", "environment-jobs"])
def test_storage_preserves_unmounted_original_files_before_allocating_or_mounting(
    tmp_path, location
):
    root = tmp_path / "role space"
    home = root / "home"
    runtime = root / "environment-jobs"
    home.mkdir(parents=True)
    runtime.mkdir()
    target = root / location / "original.txt"
    target.write_text("keep original files")
    binary = tmp_path / "bin"
    binary.mkdir()
    programs = {
        "id": "echo 0\n",
        "mountpoint": (
            'case "$2" in */home) exit 0 ;; *) exit 1 ;; esac\n'
            if location != "home"
            else "exit 1\n"
        ),
        "findmnt": 'case "$*" in *FSTYPE*) echo ext4 ;; *) echo /dev/fixture-loop ;; esac\n',
        "losetup": 'echo "$YUKI_TEST_STORAGE_ROOT/home.ext4"\n',
        "chown": "exit 0\n",
        "fallocate": 'touch "$YUKI_TEST_STORAGE_ROOT/unexpected-allocation"; exit 1\n',
        "mount": 'touch "$YUKI_TEST_STORAGE_ROOT/unexpected-mount"; exit 1\n',
    }
    if location != "home":
        (root / "home.ext4").write_bytes(b"fixture mounted home")
    for name, content in programs.items():
        path = binary / name
        path.write_text("#!/bin/sh\n" + content)
        path.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "deploy/sandbox/environment-storage.sh"), str(root)],
        env={
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "YUKI_TEST_STORAGE_ROOT": str(root),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "contains files" in result.stderr
    assert target.read_text() == "keep original files"
    assert not (root / "unexpected-allocation").exists()
    assert not (root / "unexpected-mount").exists()
