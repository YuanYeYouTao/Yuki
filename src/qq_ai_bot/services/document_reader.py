"""Bounded attachment parsing in a disposable subprocess; never execute content."""

from __future__ import annotations

import codecs
import json
import logging
import sys
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring
from pypdf import PdfReader

TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".sql",
        ".sh",
        ".ps1",
        ".ini",
        ".conf",
        ".c",
        ".cpp",
        ".h",
        ".rs",
        ".go",
        ".java",
    }
)


def _xml(archive: zipfile.ZipFile, name: str) -> Element:
    return fromstring(archive.read(name), forbid_dtd=True)


def read_document(path: Path, suffix: str, limit: int, page_limit: int) -> dict[str, object]:
    """Return a bounded prefix with explicit coverage; input is a private temp file."""
    chunks: list[str] = []
    size = 0
    truncated = False

    def append(text: str) -> bool:
        nonlocal size, truncated
        remaining = limit - size
        chunks.append(text[:remaining])
        size += min(len(text), remaining)
        if len(text) > remaining:
            truncated = True
        return size < limit

    with path.open("rb") as stream:
        header = stream.read(12)
    kind = "text"
    units = 0
    total: int | None = None
    if header.startswith(b"%PDF-"):
        kind = "pdf"
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted_document")
        total = len(reader.pages)
        for index in range(min(total, page_limit)):
            page = reader.pages[index]
            units += 1
            if not append(f"\n[page {index + 1}]\n" + (page.extract_text() or "")):
                truncated = units < total or truncated
                break
        truncated = truncated or units < total
        append("\n[PDF text extraction only; embedded images were not read.]\n")
    elif header.startswith(b"PK"):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 2048 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                raise ValueError("archive_expansion_limit")
            if any(e.flag_bits & 1 or e.file_size > max(1, e.compress_size) * 200 for e in entries):
                raise ValueError("unsafe_archive")
            names = set(archive.namelist())
            if "word/document.xml" in names:
                kind = "docx"
                root = _xml(archive, "word/document.xml")
                for paragraph in root.iter(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                ):
                    text = "".join(paragraph.itertext())
                    units += 1
                    if not append(text + "\n"):
                        truncated = True
                        break
            elif "xl/workbook.xml" in names:
                kind = "xlsx"
                ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                shared = []
                if "xl/sharedStrings.xml" in names:
                    shared = ["".join(x.itertext()) for x in _xml(archive, "xl/sharedStrings.xml")]
                relations = {r.get("Id"): r for r in _xml(archive, "xl/_rels/workbook.xml.rels")}
                sheets = []
                for sheet in _xml(archive, "xl/workbook.xml").iter(ns + "sheet"):
                    relationship = relations[
                        sheet.get(
                            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                        )
                    ]
                    target = relationship.get("Target", "")
                    member = target.lstrip("/") if target.startswith("/") else "xl/" + target
                    if (
                        relationship.get("TargetMode") == "External"
                        or ".." in member.split("/")
                        or not member.startswith("xl/worksheets/")
                        or member not in names
                    ):
                        raise ValueError("invalid_sheet_reference")
                    sheets.append((sheet.get("name", ""), member))
                total = len(sheets)
                for title, name in sheets[:page_limit]:
                    units += 1
                    if not append(f"\n[sheet {units} {json.dumps(title)}; raw cell values]\n"):
                        break
                    for row in _xml(archive, name).iter(ns + "row"):
                        cells = []
                        for cell in row:
                            raw = cell.findtext(ns + "v") or ""
                            if cell.get("t") == "s":
                                shared_index = int(raw)
                                if not 0 <= shared_index < len(shared):
                                    raise ValueError("invalid_shared_string")
                                raw = shared[shared_index]
                            elif cell.get("t") == "inlineStr":
                                raw = "".join(cell.itertext())
                            formula = cell.findtext(ns + "f")
                            if formula is not None:
                                raw = "[formula cached value] " + raw
                            cells.append(f"{cell.get('r', '?')}={raw}")
                        if not append("\t".join(cells) + "\n"):
                            break
                    if size >= limit:
                        truncated = True
                        break
                truncated = truncated or units < total
            else:
                raise ValueError("unsupported_archive")
    elif suffix in TEXT_SUFFIXES:
        # Read only enough bytes for a bounded prefix, even for a large text file.
        with path.open("rb") as stream:
            text_bytes = stream.read(limit * 4 + 4)
        truncated = path.stat().st_size > len(text_bytes)
        if text_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = codecs.getincrementaldecoder("utf-16")().decode(text_bytes, final=not truncated)
        else:
            try:
                text = codecs.getincrementaldecoder("utf-8-sig")().decode(
                    text_bytes, final=not truncated
                )
            except UnicodeDecodeError:
                text = codecs.getincrementaldecoder("gb18030")().decode(
                    text_bytes, final=not truncated
                )
        if "\x00" in text:
            raise ValueError("binary_document")
        append(text)
        units = 1
    else:
        raise ValueError("unsupported_document")
    return {
        "kind": kind,
        "text": "".join(chunks),
        "truncated": truncated,
        "units_read": units,
        "total_units": total,
    }


def main() -> None:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    logging.disable(logging.CRITICAL)
    try:
        result = read_document(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    except Exception as exc:
        category = str(exc) if isinstance(exc, ValueError) else "document_unreadable"
        safe_categories = {
            "encrypted_document",
            "archive_expansion_limit",
            "unsafe_archive",
            "unsupported_archive",
            "unsupported_document",
            "binary_document",
        }
        result = {"error": category if category in safe_categories else "document_unreadable"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
