"""Native wheel metadata describes the architecture actually passed to the compiler."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = runpy.run_path(str(ROOT / "scripts/build_vec1.py"))


@pytest.mark.parametrize(
    ("architecture", "python_platform", "target", "tag", "variants"),
    [
        ("arm64", "macosx-10.9-universal2", "11.0", "macosx_11_0_arm64", 1),
        ("x86_64", "macosx-10.9-universal2", "10.9", "macosx_10_9_x86_64", 2),
        ("arm64", "macosx-14.0-arm64", "14.0", "macosx_14_0_arm64", 1),
    ],
)
def test_macos_uses_one_architecture_and_the_same_compile_link_target(
    monkeypatch, tmp_path, architecture, python_platform, target, tag, variants
):
    monkeypatch.setattr(BUILD["sys"], "platform", "darwin")
    monkeypatch.setattr(BUILD["platform"], "machine", lambda: architecture)
    monkeypatch.setattr(BUILD["sysconfig"], "get_platform", lambda: python_platform)
    monkeypatch.setenv("CC", "cc")
    metadata = BUILD["native_build_metadata"]()
    assert metadata.architecture == architecture
    assert metadata.macosx_target == target
    assert metadata.platform_tag == tag
    commands = []
    monkeypatch.setattr(
        BUILD["subprocess"], "run", lambda command, **_kwargs: commands.append(command)
    )
    library = BUILD["build_vec1"](ROOT, tmp_path, metadata=metadata)
    assert library.name == "vec1.dylib"
    assert len(commands) == variants + 1
    assert "-dynamiclib" in commands[-1]
    for command in commands:
        assert command[command.index("-arch") + 1] == architecture
        assert f"-mmacosx-version-min={target}" in command


@pytest.mark.parametrize(
    ("system", "architecture", "python_platform", "expected"),
    [
        ("win32", "AMD64", "win-amd64", "win_amd64"),
        ("linux", "aarch64", "linux-aarch64", "linux_aarch64"),
    ],
)
def test_other_native_platform_tags_keep_the_python_abi_platform(
    monkeypatch, system, architecture, python_platform, expected
):
    monkeypatch.setattr(BUILD["sys"], "platform", system)
    monkeypatch.setattr(BUILD["platform"], "machine", lambda: architecture)
    monkeypatch.setattr(BUILD["sysconfig"], "get_platform", lambda: python_platform)
    metadata = BUILD["native_build_metadata"]()
    assert metadata.platform_tag == expected
    assert metadata.macosx_target is None
