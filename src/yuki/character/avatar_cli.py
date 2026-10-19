"""CLI center crop with EXIF orientation, sRGB pixels and no source metadata."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, PngImagePlugin


def normalized_png(path: Path) -> bytes:
    # CLI input has no arbitrary pixel ceiling; actual decoding/allocation errors
    # are reported by Pillow. The HTTP service accepts only the 1024-square output.
    previous = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if source.info.get("icc_profile"):
                converted = ImageCms.profileToProfile(
                    oriented,
                    ImageCms.ImageCmsProfile(BytesIO(source.info["icc_profile"])),
                    ImageCms.createProfile("sRGB"),
                    outputMode="RGBA",
                )
                assert converted is not None
                oriented = converted
            fitted = ImageOps.fit(oriented.convert("RGBA"), (1024, 1024), Image.Resampling.LANCZOS)
            clean = Image.new("RGBA", (1024, 1024))
            clean.paste(fitted)
            output = BytesIO()
            color = PngImagePlugin.PngInfo()
            color.add(b"sRGB", b"\x00")
            clean.save(output, format="PNG", pnginfo=color)
            return output.getvalue()
    finally:
        Image.MAX_IMAGE_PIXELS = previous
