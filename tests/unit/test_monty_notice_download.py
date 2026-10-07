from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.error
from pathlib import Path

import pytest
from scripts import export_monty_notices as notices


def test_pinned_notice_read_retries_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[str, int]] = []
    pauses: list[int] = []

    def read(url: str, *, timeout: int) -> io.BytesIO:
        attempts.append((url, timeout))
        if len(attempts) < 3:
            raise urllib.error.URLError("TLS connection interrupted")
        return io.BytesIO(b"original license text")

    monkeypatch.setattr(notices.urllib.request, "urlopen", read)
    monkeypatch.setattr(notices.time, "sleep", pauses.append)
    url = "https://raw.githubusercontent.com/example/project/" + "a" * 40 + "/LICENSE"
    assert notices.read_upstream(url) == b"original license text"
    assert attempts == [(url, 30)] * 3
    assert pauses == [1, 2]


@pytest.mark.parametrize("status", [403, 404, 503])
def test_notice_absence_is_distinct_from_failed_audit(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    attempts = 0

    def read(url: str, *, timeout: int) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(url, status, "fixture", {}, None)

    monkeypatch.setattr(notices.urllib.request, "urlopen", read)
    monkeypatch.setattr(notices.time, "sleep", lambda _: None)
    if status == 404:
        assert notices.read_upstream("https://example.invalid/LICENSE") is None
    else:
        with pytest.raises(urllib.error.HTTPError) as failure:
            notices.read_upstream("https://example.invalid/LICENSE")
        assert failure.value.code == status
    assert attempts == (3 if status == 503 else 1)


def archive_fixture(
    tmp_path: Path,
    notice_name: str,
    content: str,
    *,
    license_id: str = "MIT OR Apache-2.0",
) -> tuple[Path, str]:
    archive = tmp_path / "fixture-1.0.0.crate"
    manifest = f'[package]\nname="fixture"\nversion="1.0.0"\nlicense="{license_id}"\n'
    with tarfile.open(archive, "w:gz") as files:
        for name, value in (("Cargo.toml", manifest), (notice_name, content)):
            data = value.encode()
            member = tarfile.TarInfo("fixture-1.0.0/" + name)
            member.size = len(data)
            files.addfile(member, io.BytesIO(data))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_license_embedded_in_authors_preserves_original_terms_and_copyright(
    tmp_path: Path,
) -> None:
    original = (Path(__file__).resolve().parents[2] / "vendor/monty/LICENSE").read_text()
    content = "AUTHORS-MIT:\n" + original + "\nAUTHORS:\nfixture contributor\n"
    archive, digest = archive_fixture(tmp_path, "AUTHORS", content)
    manifest, found, vcs = notices.archive_metadata(archive, digest, "fixture-1.0.0")
    assert found == [("AUTHORS", content)]
    assert manifest["license"] == "MIT OR Apache-2.0"
    assert vcs is None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("AUTHORS", "fixture contributor\n"),
        ("README.md", "This project is licensed under MIT.\n"),
        ("AUTHORS", "Permission is hereby granted, free of charge\nCopyright fixture\n"),
    ],
)
def test_attribution_or_partial_grant_is_not_a_complete_notice(
    tmp_path: Path, name: str, content: str
) -> None:
    archive, digest = archive_fixture(tmp_path, name, content)
    _, found, _ = notices.archive_metadata(archive, digest, "fixture-1.0.0")
    assert found == []


def test_notice_is_not_read_from_an_archive_with_wrong_checksum(tmp_path: Path) -> None:
    archive, _ = archive_fixture(tmp_path, "LICENSE", "fixture text")
    with pytest.raises(ValueError, match="cargo_archive_checksum_mismatch"):
        notices.archive_metadata(archive, "0" * 64, "fixture-1.0.0")


def test_readme_mit_example_does_not_replace_the_actual_package_license(tmp_path: Path) -> None:
    example = (Path(__file__).resolve().parents[2] / "vendor/monty/LICENSE").read_text()
    archive, digest = archive_fixture(tmp_path, "README.md", example, license_id="GPL-3.0-only")
    _, found, _ = notices.archive_metadata(archive, digest, "fixture-1.0.0")
    assert found == []


def test_verified_publication_notice_uses_fixed_revision_and_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "symbolic-common-12.18.3"
    checksum, repository, revision = notices.VERIFIED_NOTICE_REVISIONS[key]
    calls = []

    def read(repo: str, commit: str) -> list[tuple[str, str]]:
        calls.append((repo, commit))
        return [("pinned/LICENSE", "original upstream copyright and terms")]

    monkeypatch.setattr(notices, "upstream_text", read)
    cache: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for _ in range(2):
        found, kind = notices.resolve_missing_notices(
            key, {"repository": repository}, None, checksum, cache
        )
        assert found == [("pinned/LICENSE", "original upstream copyright and terms")]
        assert kind == "verified_publication_source"
    assert calls == [(repository, revision)]


@pytest.mark.parametrize("field", ["checksum", "repository"])
def test_publication_notice_cannot_be_associated_with_different_source(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    key = "symbolic-common-12.18.3"
    checksum, repository, _ = notices.VERIFIED_NOTICE_REVISIONS[key]
    if field == "checksum":
        checksum = "0" * 64
    else:
        repository = "https://github.com/another/repository"
    with pytest.raises(ValueError, match="notice_verified_source_mismatch"):
        notices.resolve_missing_notices(key, {"repository": repository}, None, checksum, {})


def test_explicit_mit_declaration_retains_standard_template_without_inventing_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "quote-use-0.8.4"
    data = b"MIT License\nCopyright (c) <year> <copyright holders>\nfixture standard terms\n"
    monkeypatch.setattr(notices, "read_upstream", lambda _: data)
    monkeypatch.setattr(notices, "SPDX_MIT_SHA256", hashlib.sha256(data).hexdigest())
    found, kind = notices.resolve_missing_notices(
        key, {"license": "MIT"}, None, notices.DECLARED_MIT_ARCHIVES[key], {}
    )
    assert found == [(notices.SPDX_MIT_URL, data.decode())]
    assert kind == "declared_mit_with_spdx_standard_text"


@pytest.mark.parametrize("license_id", [None, "Apache-2.0", "MIT OR Apache-2.0"])
def test_standard_mit_text_does_not_replace_a_different_or_unknown_license(
    license_id: str | None,
) -> None:
    key = "quote-use-0.8.4"
    with pytest.raises(ValueError, match="notice_declared_mit_mismatch"):
        notices.resolve_missing_notices(
            key, {"license": license_id}, None, notices.DECLARED_MIT_ARCHIVES[key], {}
        )


@pytest.mark.parametrize("data", [None, b"changed standard text"])
def test_missing_or_changed_standard_text_fails_the_audit(
    monkeypatch: pytest.MonkeyPatch, data: bytes | None
) -> None:
    key = "quote-use-0.8.4"
    monkeypatch.setattr(notices, "read_upstream", lambda _: data)
    with pytest.raises(ValueError, match="notice_spdx_text_mismatch"):
        notices.resolve_missing_notices(
            key, {"license": "MIT"}, None, notices.DECLARED_MIT_ARCHIVES[key], {}
        )


def test_unverified_package_remains_a_gap_even_if_it_claims_mit() -> None:
    assert notices.resolve_missing_notices("unknown-1.0", {"license": "MIT"}, None, None, {}) == (
        [],
        "unresolved",
    )
