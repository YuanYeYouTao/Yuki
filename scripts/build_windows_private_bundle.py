"""Produce a task-owned private Windows bundle; secrets never enter Git or logs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "alembic.ini",
    "migrations",
    "src",
    "config/persona.md",
    "scripts",
    "vendor",
    "frontend",
    "plugins",
)


def build(
    provider: Path,
    persona: Path,
    output: Path,
    revision: str,
    bot_qq: str,
    verification: Path | None = None,
    previous_bundle: Path | None = None,
) -> Path:
    spec = importlib.util.spec_from_file_location(
        "windows_deployment", ROOT / "deploy/windows/deployment.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not re.fullmatch(r"[1-9][0-9]{4,19}", bot_qq):
        raise ValueError("A valid Bot QQ is required")
    credentials = module.read_credentials(provider)
    role = persona.read_text(encoding="utf-8-sig")
    if not role.strip():
        raise ValueError("Persona is empty")
    revision = subprocess.check_output(
        ["git", "rev-parse", f"{revision}^{{commit}}"],
        cwd=ROOT,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
    installer_revision = revision
    previous = None
    if previous_bundle is not None:
        previous = module.verify_bundle(previous_bundle)
        if (
            previous["schema_version"] != 1
            or previous["branch"] != "codex/pi-codemode-experiment"
            or not {"source.tar.gz", "private/provider.json", "private/persona.md"}
            <= set(previous["files"])
            or previous["bot_qq"] != bot_qq
            or not re.fullmatch(r"[0-9a-f]{40}", previous["source_revision"])
            or json.loads((previous_bundle / "private/provider.json").read_text()) != credentials
            or (previous_bundle / "private/persona.md").read_text() != role
        ):
            raise ValueError("Previous package identity or private configuration differs")
        uuid.UUID(previous["bundle_id"])
        revision = previous["source_revision"]
    output = output.resolve()
    archive = output.with_suffix(".zip")
    if output.is_relative_to(ROOT) or output.exists() or archive.exists():
        raise ValueError("Use a new private output directory outside the repository")
    output.mkdir(mode=0o700, parents=True)
    for path in (ROOT / "deploy/windows").iterdir():
        if path.is_file():
            target = output / path.name
            if path.suffix in {".cmd", ".ps1"}:
                target.write_bytes(
                    path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                )
            else:
                shutil.copyfile(path, target)
            os.chmod(target, 0o600)
    (output / "private").mkdir(mode=0o700)
    module.private_write(output / "private/provider.json", json.dumps(credentials, indent=2) + "\n")
    module.private_write(output / "private/persona.md", role)
    if previous_bundle is not None:
        shutil.copyfile(previous_bundle / "source.tar.gz", output / "source.tar.gz")
    else:
        with (output / "source.tar.gz").open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressor:
                child = subprocess.Popen(
                    ["git", "archive", "--format=tar", revision, *SOURCE_PATHS],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                assert child.stdout
                with child.stdout:
                    shutil.copyfileobj(child.stdout, compressor)
                if child.wait() != 0:
                    raise ValueError("Unable to archive the pinned source")
    os.chmod(output / "source.tar.gz", 0o600)
    if verification is not None:
        module.private_write(output / "verification.json", verification.read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "bundle_id": previous["bundle_id"] if previous else str(uuid.uuid4()),
        "source_revision": revision,
        "installer_revision": installer_revision,
        "branch": "codex/pi-codemode-experiment",
        "migration_head": "0096",
        "bot_qq": bot_qq,
        "files": {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
    }
    if previous is not None and previous_bundle is not None:
        manifest["previous_files"] = previous["files"]
        manifest["previous_manifest_sha256"] = hashlib.sha256(
            (previous_bundle / "manifest.json").read_bytes()
        ).hexdigest()
    module.private_write(output / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    module.verify_bundle(output)
    with archive.open("xb") as stream:
        os.chmod(archive, 0o600)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path in sorted(output.rglob("*")):
                if path.is_file():
                    package.write(path, output.name + "/" + str(path.relative_to(output)))
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError("ZIP verification failed")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-file", type=Path, required=True)
    parser.add_argument("--persona-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--bot-qq", required=True)
    parser.add_argument("--verification-file", type=Path)
    parser.add_argument("--previous-bundle", type=Path)
    args = parser.parse_args()
    try:
        archive = build(
            args.provider_file,
            args.persona_file,
            args.output,
            args.revision,
            args.bot_qq,
            args.verification_file,
            args.previous_bundle,
        )
    except Exception as exc:
        raise SystemExit(
            f"Private package build failed ({type(exc).__name__}); credentials hidden"
        ) from None
    print(
        json.dumps(
            {
                "archive": str(archive),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "bytes": archive.stat().st_size,
            }
        )
    )


if __name__ == "__main__":
    main()
