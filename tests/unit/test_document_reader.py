"""Supported formats share bounded, non-executing attachment parsing."""

import zipfile
from pathlib import Path

import pytest
from defusedxml.common import DTDForbidden
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from qq_ai_bot.services.document_reader import read_document


def test_document_formats_and_read_boundaries(tmp_path: Path) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("中文记录" * 100, encoding="utf-8")
    result = read_document(text, ".txt", 17, 2)
    assert result["text"] == ("中文记录" * 100)[:17]
    assert result["truncated"] is True

    docx = tmp_path / "notes.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>docx fact</w:t></w:r></w:p></w:body></w:document>",
        )
    assert "docx fact" in str(read_document(docx, ".docx", 1000, 2)["text"])

    xlsx = tmp_path / "notes.xlsx"
    with zipfile.ZipFile(xlsx, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Data" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row><c r="A1" t="inlineStr"><is><t>xlsx fact</t></is></c>'
            '<c r="B1"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>',
        )
    result = read_document(xlsx, ".xlsx", 1000, 2)
    assert "xlsx fact" in str(result["text"])
    assert "[formula cached value] 3" in str(result["text"])

    pdf = tmp_path / "notes.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (pdf fact) Tj ET")
    page[NameObject("/Contents")] = stream
    writer.add_blank_page(width=200, height=200)
    writer.write(pdf)
    result = read_document(pdf, ".pdf", 1000, 1)
    assert "pdf fact" in str(result["text"])
    assert result["truncated"] is True and result["units_read"] == 1
    writer.encrypt("private-test")
    writer.write(pdf)
    with pytest.raises(ValueError, match="encrypted_document"):
        read_document(pdf, ".pdf", 1000, 1)

    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
        )
    with pytest.raises(DTDForbidden):
        read_document(docx, ".docx", 1000, 1)
    with zipfile.ZipFile(docx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 500_000)
    with pytest.raises(ValueError, match="unsafe_archive"):
        read_document(docx, ".docx", 1000, 1)
    text.write_bytes(b"MZ\x00binary")
    with pytest.raises(ValueError, match="unsupported_document"):
        read_document(text, ".exe", 1000, 1)
