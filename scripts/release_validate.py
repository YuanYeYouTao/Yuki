"""Validate the immutable Yuki release identity before expensive CI work."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import tomllib
from pathlib import Path

_TAG_PATTERN = re.compile(r"^v(?P<version>0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_APP_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"$', re.MULTILINE)
_PLUGIN_API_PATTERN = re.compile(r'^PLUGIN_API_VERSION\s*=\s*"([^"]+)"$', re.MULTILINE)


class ReleaseValidationError(ValueError):
    """Raised when a release identity or invariant is inconsistent."""


def _match_value(path: Path, pattern: re.Pattern[str], label: str) -> str:
    match = pattern.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseValidationError(f"could not read {label} from {path}")
    return match.group(1)


def project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def locked_project_version(root: Path, package_name: str = "qq-ai-bot") -> str:
    with (root / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    matches = [
        str(package["version"])
        for package in lock["package"]
        if package.get("name") == package_name and package.get("source", {}).get("editable") == "."
    ]
    if len(matches) != 1:
        raise ReleaseValidationError(
            f"{root / 'uv.lock'} must contain one editable {package_name} package"
        )
    return matches[0]


def validate_release_identity(root: Path, tag: str) -> str:
    tag_match = _TAG_PATTERN.fullmatch(tag)
    if tag_match is None:
        raise ReleaseValidationError(f"release tag must match vX.Y.Z exactly: {tag!r}")
    tag_version = tag.removeprefix("v")
    versions = {
        "tag": tag_version,
        "pyproject": project_version(root),
        "runtime": _match_value(
            root / "src/qq_ai_bot/__init__.py", _APP_VERSION_PATTERN, "runtime version"
        ),
        "uv.lock": locked_project_version(root),
        "install.sh": _match_value(
            root / "install.sh",
            re.compile(r'^VERSION="([^"]+)"$', re.MULTILINE),
            "installer version",
        ),
        "install.ps1": _match_value(
            root / "install.ps1",
            re.compile(r'\[string\]\$Version = "([^"]+)"'),
            "installer version",
        ),
    }
    if set(versions.values()) != {tag_version}:
        rendered = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise ReleaseValidationError(f"release versions do not match: {rendered}")

    worker_version = project_version(root / "services/genie_tts_worker")
    if worker_version != "1.9.0":
        raise ReleaseValidationError(
            f"Genie-TTS Worker component version must remain 1.9.0, got {worker_version}"
        )
    worker_lock_version = locked_project_version(
        root / "services/genie_tts_worker", "genie-tts-worker"
    )
    if worker_lock_version != worker_version:
        raise ReleaseValidationError(
            "Genie-TTS Worker pyproject.toml and uv.lock versions do not match: "
            f"{worker_version} != {worker_lock_version}"
        )
    # This early release gate runs without project dependencies installed.
    # Read literal Alembic revision metadata from the shipped migrations.
    revisions: set[str] = set()
    parents: set[str] = set()
    for migration in (root / "migrations/versions").glob("*.py"):
        for node in ast.parse(migration.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name, value = node.targets[0].id, node.value
            else:
                continue
            if name not in {"revision", "down_revision"} or value is None:
                continue
            literal = ast.literal_eval(value)
            if name == "revision":
                if literal in revisions:
                    raise ReleaseValidationError(f"Duplicate migration revision: {literal}")
                revisions.add(literal)
            elif literal is not None:
                parents.update((literal,) if isinstance(literal, str) else literal)
    if len(revisions - parents) != 1 or parents - revisions:
        raise ReleaseValidationError("Bundled migrations must have one head and no missing parents")
    head = next(iter(revisions - parents))
    baseline = f"<!-- release-baseline: version={tag_version} schema={head} -->"
    for relative in (
        "README.md",
        f"docs/releases/v{tag_version}.md",
        f"docs/upgrade-{tag_version}.md",
    ):
        path = root / relative
        if not path.is_file() or baseline not in path.read_text(encoding="utf-8"):
            raise ReleaseValidationError(
                f"release/schema baseline is stale: {relative}; expected {baseline}"
            )
    plugin_api = _match_value(
        root / "src/yuki_plugin_sdk/api.py", _PLUGIN_API_PATTERN, "Plugin API version"
    )
    if plugin_api != "2.0":
        raise ReleaseValidationError(f"Plugin API must remain 2.0, got {plugin_api}")
    return tag_version


def validate_tag_commit(root: Path, tag: str, main_ref: str) -> None:
    head = _git(root, "rev-parse", "HEAD")
    tag_commit = _git(root, "rev-parse", f"{tag}^{{commit}}")
    if tag_commit != head:
        raise ReleaseValidationError(f"{tag} resolves to {tag_commit}, but checkout is {head}")
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_commit, main_ref],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ReleaseValidationError(f"{tag} commit {tag_commit} is not reachable from {main_ref}")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write_github_output(version: str, path: str | None) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"version={version}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-main-ancestor", action="store_true")
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT"))
    args = parser.parse_args()
    root = args.root.resolve()
    version = validate_release_identity(root, args.tag)
    if args.require_main_ancestor:
        validate_tag_commit(root, args.tag, args.main_ref)
    _write_github_output(version, args.github_output)
    print(f"validated Yuki {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
