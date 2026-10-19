"""Record the installed client package and exact dependency lock in its private venv."""

import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def manifest(lock: Path, source: str) -> dict[str, Any]:
    package = importlib.metadata.distribution("yuki")
    record = package.read_text("RECORD")
    if record is None:
        raise ValueError("installed_client_record_missing")
    return {
        "version": package.version,
        "source_commit": source,
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "record_sha256": hashlib.sha256(record.encode()).hexdigest(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "packages": [
            {
                "name": item.metadata["Name"],
                "version": item.version,
                "record_sha256": hashlib.sha256(
                    (item.read_text("RECORD") or "").encode()
                ).hexdigest(),
            }
            for item in sorted(
                importlib.metadata.distributions(), key=lambda value: value.metadata["Name"].lower()
            )
        ],
    }


def verify_records(packages: Iterable[importlib.metadata.Distribution]) -> None:
    for package in packages:
        record = package.read_text("RECORD")
        if record is None:
            raise ValueError("installed_client_record_missing")
        # Distribution.files may filter missing entries; the wheel record is the
        # source of expected files, including entries lost during an interrupted copy.
        for name, checksum, _size in csv.reader(io.StringIO(record)):
            if not checksum:
                continue
            algorithm, expected = checksum.split("=", 1)
            digest = hashlib.new(algorithm, package.locate_file(name).read_bytes()).digest()
            encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if encoded != expected:
                raise ValueError(f"installed_client_file_changed: {name}")


def verify(root: Path) -> None:
    value = json.loads((root / "INSTALLATION.json").read_text())
    package = importlib.metadata.distribution("yuki")
    if sys.version_info[:2] != (3, 12) or package.version != value["version"]:
        raise ValueError("client_version_mismatch")
    record = package.read_text("RECORD")
    if record is None or hashlib.sha256(record.encode()).hexdigest() != value["record_sha256"]:
        raise ValueError("client_record_mismatch")
    for expected in value["packages"]:
        installed = importlib.metadata.distribution(expected["name"])
        installed_record = installed.read_text("RECORD")
        if (
            installed.version != expected["version"]
            or installed_record is None
            or hashlib.sha256(installed_record.encode()).hexdigest() != expected["record_sha256"]
        ):
            raise ValueError("client_dependency_record_mismatch")
        verify_records([installed])


if __name__ == "__main__":
    root = Path(__file__).parent
    if sys.argv[1] == "--verify":
        verify(root)
    else:
        target = root / "INSTALLATION.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(manifest(Path(sys.argv[1]), sys.argv[2]), stream, sort_keys=True)
            stream.write("\n")
