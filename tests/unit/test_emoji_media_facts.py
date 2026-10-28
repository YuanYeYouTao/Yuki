"""Header naming and the TGS prefix sniff do not materialize the whole original."""

from __future__ import annotations

import gzip
import io
import tracemalloc

from PIL import Image

from yuki.emoji.library import _naming_facts
from yuki.emoji.media import _tgs_header, _TgsSniff, header_facts


def _tgs(payload: bytes) -> dict | None:
    return _tgs_header(io.BytesIO(gzip.compress(payload)))


def test_tgs_sniff_reads_root_fields_across_chunks_and_escapes():
    text = (
        b'{"layers":[1],"\\u0068":9,"\\\\nested":{"v":false,"layers":[0],"w":8,"h":8},'
        b'"\\u0076":"5.7","w":2}'
    )
    sniff = _TgsSniff()
    for index in range(0, len(text), 3):
        sniff.feed(text[index : index + 3])
    assert sniff.saw_v and sniff.saw_layers
    assert sniff.width == 2 and sniff.height == 9

    header = _tgs(b'{"h":4,"layers":[{"v":"no","w":1,"h":1}],"v":"5.7","w":3}')
    assert header is not None
    assert header["image_format"] == "TGS"
    assert header["width"] == 3 and header["height"] == 4


def test_tgs_sniff_rejects_non_objects_and_keeps_a_damaged_tail():
    assert _tgs(b"[1,2,3]") is None
    assert _tgs(b"hello") is None
    assert _tgs_header(io.BytesIO(b"\x1f\x8b\x08not-json")) is None
    assert _tgs_header(io.BytesIO(gzip.compress(b'{"v":"5","layers":[]}')[:10])) is None

    missing = _tgs(b'{"v":"5.7","layers":[]}')
    assert missing is not None
    assert missing["width"] is None and missing["height"] is None

    padded = b'{"v":"5.7","layers":[],"w":1,"h":2,"pad":"' + (b"x" * 5000) + b'"}'
    damaged = bytearray(gzip.compress(padded))
    damaged[-1] ^= 0xFF
    recognized = _tgs_header(io.BytesIO(damaged))
    assert recognized is not None and recognized["width"] == 1 and recognized["height"] == 2
    unscored = b'{"v":"5.7","layers":[],"pad":"' + (b"x" * 5000) + b'"}'
    damaged_tail = bytearray(gzip.compress(unscored))
    damaged_tail[-1] ^= 0xFF
    kept = _tgs_header(io.BytesIO(damaged_tail))
    assert kept is not None and kept["width"] is None and kept["height"] is None


def test_large_unrelated_json_is_not_copied_as_one_object():
    payload = b'{"pad":"' + (b"x" * (8 * 1024 * 1024)) + b'","v":"5.7","layers":[1],"w":1,"h":2}'
    blob = gzip.compress(payload, compresslevel=9)
    assert len(blob) < 64 * 1024
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    try:
        header = _tgs_header(io.BytesIO(blob))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert header is not None and header["image_format"] == "TGS"
    assert peak - baseline < 1024 * 1024


def test_header_names_pillow_formats_without_enumerating_gif_frames(tmp_path, monkeypatch):
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, "PPM")
    named = header_facts(io.BytesIO(stream.getvalue()))
    assert named is not None and named["image_format"] == "PPM"

    unknown = tmp_path / "notes.bin"
    unknown.write_bytes(b"\x00\x01not-an-image")
    assert header_facts(unknown.open("rb")) is None
    assert _naming_facts(unknown)["image_format"] == "BIN"

    gif = io.BytesIO()
    Image.new("P", (1, 1), 1).save(gif, "GIF")
    opened = {"frames": 0}

    class Guard:
        format = "GIF"
        width = 1
        height = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def n_frames(self):
            opened["frames"] += 1
            raise AssertionError("gif frames were enumerated")

        @property
        def is_animated(self):
            opened["frames"] += 1
            raise AssertionError("gif frames were enumerated")

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: Guard())
    facts = header_facts(io.BytesIO(gif.getvalue()))
    assert facts is not None and facts["image_format"] == "GIF"
    assert opened["frames"] == 0


def test_tgs_dimensions_use_json_number_grammar_and_skip_unrelated_numbers():
    payload = b'{"unused":' + b"1" * (8 * 1024 * 1024) + b',"w":128.0,"h":1e3,"layers":[],"v":"5"}'
    header = _tgs(payload)
    assert header is not None and header["width"] == 128 and header["height"] == 1000
    sniff = _TgsSniff()
    text = b'{"w":1.28e+2,"h":2.56E2,"layers":[],"v":"5"}'
    for byte in text:
        sniff.feed(bytes((byte,)))
    assert sniff.width == 128 and sniff.height == 256


def test_tgs_late_facts_survive_bad_crc_and_unknown_escaped_keys():
    payload = (
        b'{"pad":"' + b"x" * (8 * 1024 * 1024) + b'","\\u0076":"5","layers":[],"w":128.0,"h":1e3}'
    )
    damaged = bytearray(gzip.compress(payload))
    damaged[-8] ^= 0xFF
    header = _tgs_header(io.BytesIO(damaged))
    assert header is not None and header["width"] == 128 and header["height"] == 1000
    assert _tgs(b'{"nested":{"v":"5","layers":[],"w":1,"h":2}}') is None


def test_tgs_missing_gzip_trailer_keeps_all_payload_and_optional_header_fields():
    payload = b'{"v":"5","layers":[],"w":128.0,"h":1e3}'
    blob = gzip.compress(payload)
    header = _tgs_header(io.BytesIO(blob[:-8]))
    assert header is not None and header["width"] == 128 and header["height"] == 1000
    short = _tgs_header(io.BytesIO(gzip.compress(b'{"v":"5","layers":[]}')[:-8]))
    assert short is not None
    optional = bytearray(blob[:10])
    optional[3] |= 4 | 8 | 16 | 2
    encoded = bytes(optional) + b"\x03\x00xyz" + b"name.tgs\0comment\0\0\0" + blob[10:]
    named = _tgs_header(io.BytesIO(encoded))
    assert named is not None and named["width"] == 128 and named["height"] == 1000
