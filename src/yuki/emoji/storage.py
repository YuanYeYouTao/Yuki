"""Atomic, format-aware filesystem storage for original emoji media and previews."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import warnings
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from yuki.emoji.models import StoredEmojiMedia

_FORMAT_INFO: Final[dict[str, tuple[str, str]]] = {
    "GIF": ("gif", "image/gif"),
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}


class EmojiStorageError(RuntimeError):
    """Sanitized local media-storage error."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class EmojiStorage:
    """Persist immutable originals and deterministic first-frame WebP previews."""

    def __init__(self, root: Path | str = Path("data/emoji"), *, preview_max_dimension: int = 512):
        if preview_max_dimension <= 0:
            raise ValueError("preview_max_dimension must be positive")
        self.root = Path(root)
        self.original_root = self.root / "original"
        self.preview_root = self.root / "preview"
        self._preview_max_dimension = preview_max_dimension

    def inspect(self, content: bytes, *, near_duplicate_enabled: bool) -> StoredEmojiMedia:
        """Validate bytes, derive real format, and describe their final storage paths."""

        if not content:
            raise EmojiStorageError("empty_media", "表情图片为空")
        sha256 = hashlib.sha256(content).hexdigest()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    image_format = (image.format or "").upper()
                    info = _FORMAT_INFO.get(image_format)
                    if info is None:
                        raise EmojiStorageError("unsupported_format", "仅支持 PNG/JPEG/GIF/WebP")
                    width, height = image.size
                    frame_count = int(getattr(image, "n_frames", 1) or 1)
                    if width <= 0 or height <= 0 or frame_count <= 0:
                        raise EmojiStorageError("invalid_geometry", "表情图片尺寸或帧数无效")
                    perceptual_hash = (
                        self._difference_hash(image) if near_duplicate_enabled else None
                    )
        except EmojiStorageError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise EmojiStorageError("decompression_bomb", "表情图片像素规模不安全") from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise EmojiStorageError("corrupt_image", "表情图片损坏或无法解析") from exc
        extension, mime_type = info
        return StoredEmojiMedia(
            sha256=sha256,
            relative_path=f"original/{sha256[:2]}/{sha256}.{extension}",
            preview_relative_path=f"preview/{sha256[:2]}/{sha256}.webp",
            image_format=image_format,
            mime_type=mime_type,
            byte_size=len(content),
            width=width,
            height=height,
            frame_count=frame_count,
            animated=frame_count > 1,
            perceptual_hash=perceptual_hash,
        )

    def persist(self, content: bytes, media: StoredEmojiMedia) -> None:
        """Atomically persist an original and its preview; existing exact files are reused."""

        if hashlib.sha256(content).hexdigest() != media.sha256:
            raise EmojiStorageError("hash_mismatch", "表情内容校验失败")
        original = self.resolve(media.relative_path)
        preview = self.resolve(media.preview_relative_path)
        self._atomic_write(original, content)
        if not preview.exists():
            self._atomic_write(preview, self._build_preview(content))

    def restore_preview(self, asset_path: str, preview_path: str) -> None:
        original = self.resolve(asset_path)
        if not original.is_file():
            raise EmojiStorageError("missing_original", "表情原文件不存在")
        self._atomic_write(self.resolve(preview_path), self._build_preview(original.read_bytes()))

    def read(self, relative_path: str) -> bytes:
        path = self.resolve(relative_path)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise EmojiStorageError("missing_original", "表情原文件不存在") from exc
        except OSError as exc:
            raise EmojiStorageError("read_failed", "表情原文件读取失败") from exc

    def exists(self, relative_path: str | None) -> bool:
        return bool(relative_path and self.resolve(relative_path).is_file())

    def remove(self, relative_path: str | None) -> bool:
        if not relative_path:
            return False
        path = self.resolve(relative_path)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def cleanup_temporary_files(self) -> int:
        if not self.root.exists():
            return 0
        deleted = 0
        for path in self.root.rglob(".emoji-*.tmp"):
            if path.is_file():
                path.unlink()
                deleted += 1
        return deleted

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise EmojiStorageError("invalid_path", "表情存储路径越界")
        return candidate

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".emoji-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _build_preview(self, content: bytes) -> bytes:
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.seek(0)
                frame = ImageOps.exif_transpose(image.copy()).convert("RGBA")
                frame.thumbnail(
                    (self._preview_max_dimension, self._preview_max_dimension),
                    Image.Resampling.LANCZOS,
                )
                output = io.BytesIO()
                frame.save(output, format="WEBP", lossless=True, method=4)
                return output.getvalue()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise EmojiStorageError("preview_failed", "表情预览生成失败") from exc

    @staticmethod
    def _difference_hash(image: Image.Image) -> str:
        image.seek(0)
        gray = ImageOps.grayscale(image.copy()).resize((9, 8), Image.Resampling.LANCZOS)
        pixels = gray.load()
        if pixels is None:
            raise EmojiStorageError("hash_failed", "无法读取表情像素")
        bits = 0
        for row in range(8):
            for column in range(8):
                left_value = pixels[column, row]
                right_value = pixels[column + 1, row]
                if not isinstance(left_value, int | float) or not isinstance(
                    right_value, int | float
                ):
                    raise EmojiStorageError("hash_failed", "表情灰度像素格式无效")
                left = float(left_value)
                right = float(right_value)
                bits = (bits << 1) | int(left > right)
        return f"{bits:016x}"
