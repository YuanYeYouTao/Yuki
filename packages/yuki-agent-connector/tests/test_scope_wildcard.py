"""A delegation scope that grants every command (``commands: ["*"]``).

Production delegation 8331a5f7 (2026-11-19) was started with ``commands: ["*"]``
for "all commands"; the connector read ``*`` as a literal prefix, so no Bash call
was ever inside the scope and all 33 requests went back to the parent Work.
Failure cases written before the change:
1. ``*`` must allow a plain command.
2. ``*`` must allow commands with shell control (``&&``, ``|``, ``;``, ``>``,
   ``$()``, newlines): the owner granted every command, not a prefix.
3. ``*`` next to ordinary prefixes behaves the same as ``*`` alone.
4. An empty command stays outside the scope.
5. Ordinary prefixes keep their meaning: whole-word prefix only, and shell
   control still defeats a prefix.
6. ``*`` widens neither write paths nor network.
"""

from pathlib import Path

from yuki_agent_connector.scope import Scope


def _scope(*commands: str, tmp_path: Path, network: bool = False) -> Scope:
    scope = Scope.parse(
        {"write_paths": [str(tmp_path)], "network": network, "commands": list(commands)},
        str(tmp_path),
    )
    assert scope is not None
    return scope


def test_wildcard_allows_every_command(tmp_path: Path) -> None:
    scope = _scope("*", tmp_path=tmp_path)
    for command in (
        "ls",
        ["python", "-m", "pytest", "-q"],
        "cd work && .venv/bin/python -I tools/render.py pages && ls pages",
        "unzip -v pkg.zip | awk 'NR>3{print $7}' | head -12",
        "mkdir -p out; echo done > out/log.txt",
        "cat > tools/audit.py <<'EOF'\nprint(1)\nEOF",
        "echo $(date)",
    ):
        assert scope.runs(command), command
    assert _scope("pytest", "*", tmp_path=tmp_path).runs("rm -rf build && make")


def test_wildcard_keeps_empty_and_other_bounds(tmp_path: Path) -> None:
    scope = _scope("*", tmp_path=tmp_path)
    assert not scope.runs("")
    assert not scope.runs("   ")
    assert not scope.writes("/etc/hosts", str(tmp_path))
    assert scope.writes("out/report.md", str(tmp_path))
    assert scope.network is False


def test_prefixes_keep_their_meaning(tmp_path: Path) -> None:
    scope = _scope("pytest", "git commit", tmp_path=tmp_path)
    assert scope.runs("pytest -q tests")
    assert scope.runs("git commit -m msg")
    assert not scope.runs("pytestx")
    assert not scope.runs("git push")
    assert not scope.runs("pytest -q && rm -rf build")
    assert not scope.runs("pytest | tee log")
