"""Identify only safe browser raster types from bytes, never file extensions."""


def raster_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM") and len(data) >= 26 and data[6:10] == b"\0\0\0\0":
        return "image/bmp"
    if len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return "application/octet-stream"
