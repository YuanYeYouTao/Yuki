"""Manifest admission follows actual additive versus breaking SDK requirements."""

import shutil
from pathlib import Path

import pytest

from yuki.plugin_host.manifest import ManifestValidationError, load_manifest
from yuki_plugin_sdk.api import is_api_compatible


@pytest.mark.parametrize(
    ("requested", "supported"),
    [("9.0", True), ("8.0", False), ("9.1", False), ("7.0", False), ("10.0", False)],
)
def test_manifest_load_accepts_implemented_minor_contracts(tmp_path, requested, supported):
    source = Path(__file__).parents[2] / "examples/plugins/com.example.echo"
    destination = tmp_path / source.name
    shutil.copytree(source, destination)
    manifest = destination / "plugin.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf8").replace(
            'plugin_api = "9.0"', f'plugin_api = "{requested}"'
        ),
        encoding="utf8",
    )
    if supported:
        assert (
            load_manifest(destination, yuki_version="4.0.0", host_plugin_api="9.0").plugin_api
            == requested
        )
    else:
        with pytest.raises(ManifestValidationError, match="incompatible"):
            load_manifest(destination, yuki_version="4.0.0", host_plugin_api="9.0")


def test_unknown_api_syntax_and_higher_contract_are_not_satisfied():
    assert is_api_compatible("8.0", "8.1")
    assert not is_api_compatible("8.1", "8.0")
    assert not is_api_compatible("07.0", "7.1")
    assert not is_api_compatible("7.0", "broken")
