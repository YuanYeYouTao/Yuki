"""Export locked Monty dependency notices from verified Cargo archives.

Run after cargo fetch --locked. Missing license text is recorded as an audit
gap, never an inferred license grant. Upstream fallback reads public GitHub LICENSE files at
the exact packaged VCS commit and records their URLs and content hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MONTY_SHA = "3f9d6ef413fb951e5b80113b7088d535bd028fcb"
TYPESHED_SHA = "0e16ea31d2e188fdc126cb31e7c4fcc6b5a8da96"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def license_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.iterdir()
        if p.is_file() and p.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
    )


def archive_metadata(
    archive: Path, expected: str, prefix: str
) -> tuple[dict[str, Any], list[tuple[str, str]], str | None]:
    if sha256(archive) != expected:
        raise ValueError(f"cargo_archive_checksum_mismatch:{prefix}")
    with tarfile.open(archive) as files:

        def read(name: str) -> str:
            handle = files.extractfile(f"{prefix}/{name}")
            if handle is None:
                raise ValueError("cargo_archive_file_missing")
            content = handle.read(1_000_001)
            if len(content) > 1_000_000:
                raise ValueError("notice_text_too_large")
            return content.decode("utf-8")

        manifest = tomllib.loads(read("Cargo.toml"))["package"]
        notices = []
        for member in files.getmembers():
            if not member.isfile() or not member.name.startswith(prefix + "/"):
                continue
            path = Path(member.name.removeprefix(prefix + "/"))
            if (
                path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
                or any(part.upper() in {"LICENSES", "LICENCES"} for part in path.parts[:-1])
                or path.as_posix() == manifest.get("license-file")
            ):
                notices.append((path.as_posix(), read(path.as_posix())))
        try:
            vcs = json.loads(read(".cargo_vcs_info.json"))["git"]["sha1"]
        except KeyError:
            vcs = None
    return manifest, sorted(notices), vcs


def read_upstream(url: str) -> bytes | None:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        # Retry only a read of the same pinned public URL. Exhaustion fails
        # the audit; a transport error is never reported as absent licensing.
        time.sleep(attempt + 1)
    raise AssertionError("unreachable_notice_retry")


def upstream_text(repository: str, commit: str) -> list[tuple[str, str]]:
    match = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+)(?:/|$)", repository)
    if match is None or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("notice_upstream_not_pinned")
    repo = match[1].removesuffix(".git")
    found = []
    for name in ("LICENSE", "LICENSE.txt", "LICENSE-MIT", "LICENSE-APACHE", "NOTICE"):
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{name}"
        content = read_upstream(url)
        if content is None:
            continue
        if len(content) > 1_000_000:
            raise ValueError("notice_text_too_large")
        found.append((url, content.decode("utf-8")))
    return found


def export(
    source: Path, cargo_home: Path, output: Path, *, target: str, worker: Path, wheel: Path
) -> dict[str, Any]:
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True)
    if commit.strip() != MONTY_SHA:
        raise ValueError("monty_source_revision_mismatch")
    lock = tomllib.loads((source / "Cargo.lock").read_text())
    workspace = tomllib.loads((source / "Cargo.toml").read_text())["workspace"]["package"]
    local = {}
    for manifest in source.glob("crates/*/Cargo.toml"):
        package = tomllib.loads(manifest.read_text())["package"]
        version = package.get("version")
        if isinstance(version, dict):
            version = workspace["version"]
        local[(package["name"], version)] = manifest.parent
    archives = {p.name: p for p in (cargo_home / "registry/cache").glob("*/*.crate")}
    built = set()
    for package, extra in (
        ("monty-runtime", ["--no-default-features"]),
        ("pydantic-monty-client", []),
    ):
        tree = subprocess.check_output(
            [
                "cargo",
                "tree",
                "--locked",
                "--target",
                target,
                "--edges",
                "normal,build",
                "--prefix",
                "none",
                "--format",
                "{p}",
                "-p",
                package,
                *extra,
            ],
            cwd=source,
            text=True,
        )
        for line in tree.splitlines():
            match = re.match(r"^(\S+) v(\S+)", line)
            if match:
                built.add((match[1], match[2]))
    texts: dict[str, dict[str, str]] = {}
    fetched: dict[tuple[str, str], list[tuple[str, str]]] = {}
    packages = []
    gaps = []
    for package in sorted(lock["package"], key=lambda p: (p["name"], p["version"])):
        name, version = package["name"], package["version"]
        key = f"{name}-{version}"
        registry = "checksum" in package
        if registry:
            manifest, notices, vcs = archive_metadata(
                archives[key + ".crate"], package["checksum"], key
            )
        else:
            root = local[(name, version)]
            manifest = tomllib.loads((root / "Cargo.toml").read_text())["package"]
            notices = [(p.name, p.read_text()) for p in license_files(root)]
            vcs = MONTY_SHA
        license_id = manifest.get("license")
        if isinstance(license_id, dict):
            license_id = workspace["license"]
        if not notices and not registry:
            notices = [("Monty/LICENSE", (source / "LICENSE").read_text())]
        if not notices:
            repository = manifest.get("repository")
            if isinstance(repository, str) and isinstance(vcs, str):
                identity = (repository, vcs)
                if identity not in fetched:
                    fetched[identity] = upstream_text(repository, vcs)
                notices = fetched[identity]
        if not notices:
            gaps.append(
                {
                    "package": key,
                    "declared_license": license_id,
                    "repository": repository,
                    "vcs_commit": vcs,
                    "reason": (
                        "Published archive and checked pinned upstream LICENSE locations "
                        "omit license text; no copyright statement synthesized."
                        if vcs is not None
                        else "Published archive omits license text and provides no exact VCS "
                        "revision for upstream fallback; no copyright statement synthesized."
                    ),
                }
            )
        notice_refs = []
        for origin, content in notices:
            digest = hashlib.sha256(content.encode()).hexdigest()
            texts.setdefault(digest, {"sha256": digest, "text": content})
            notice_refs.append({"source": origin, "sha256": digest})
        packages.append(
            {
                "name": name,
                "version": version,
                "declared_license": license_id,
                "source": package.get("source", f"https://github.com/pydantic/monty@{MONTY_SHA}"),
                "archive_sha256": package.get("checksum"),
                "in_worker_or_binding_target_tree": (name, version) in built,
                "notices": notice_refs,
            }
        )
    typeshed = upstream_text("https://github.com/python/typeshed", TYPESHED_SHA)
    typeshed_refs = []
    for origin, content in typeshed:
        digest = hashlib.sha256(content.encode()).hexdigest()
        texts.setdefault(digest, {"sha256": digest, "text": content})
        typeshed_refs.append({"source": origin, "sha256": digest})
    report = {
        "format": "yuki_monty_notices_v1",
        "source_sha": MONTY_SHA,
        "cargo_lock_sha256": sha256(source / "Cargo.lock"),
        "target": target,
        "worker_sha256": sha256(worker),
        "wheel_sha256": sha256(wheel),
        "rust_toolchain": "1.96.0",
        "maturin_version": "1.9.6",
        "patch_sha256": sha256(
            Path(__file__).resolve().parents[1]
            / "vendor/patches/monty-3f9d6ef-string-cache-iterator.patch"
        ),
        "license_scope": (
            "All locked packages; target membership records normal/build dependency trees, "
            "not legal license selection."
        ),
        "packages": packages,
        "license_text_audit_complete": not gaps,
        "license_text_gaps": gaps,
        "embedded_typeshed": {"source_sha": TYPESHED_SHA, "notices": typeshed_refs},
        "notice_texts": sorted(texts.values(), key=lambda p: p["sha256"]),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return {
        "locked_packages": len(packages),
        "target_packages": len(built),
        "notice_texts": len(texts),
        "license_text_gaps": len(gaps),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cargo-home", type=Path, default=Path.home() / ".cargo")
    parser.add_argument("--target", required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    print(
        export(
            args.source,
            args.cargo_home,
            args.output,
            target=args.target,
            worker=args.worker,
            wheel=args.wheel,
        )
    )


if __name__ == "__main__":
    main()
