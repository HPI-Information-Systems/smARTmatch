"""Decode blocking input images with the formats accepted by DINOv3."""

from __future__ import annotations

from os import PathLike

from PIL import Image, UnidentifiedImageError

# Pillow selects these decoders from the file signature, not the filename suffix.
# Keep extensionless SPSG image paths supported while excluding other parsers.
INPUT_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")


class ImageDecodeError(ValueError):
    """An input file exists but Pillow cannot safely decode it for matching."""


def load_rgb_image(image_path: str | PathLike[str]) -> Image.Image:
    """Load one supported image completely and detach it from its source file."""

    source = None
    try:
        source = Image.open(image_path, formats=INPUT_IMAGE_FORMATS)
        return source.convert("RGB")
    except (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError):
        # Filesystem failures are operational errors, not bad image contents.
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ImageDecodeError(
            f"Cannot decode supported image file: {image_path}"
        ) from exc
    except OSError as exc:
        if exc.errno is not None:
            # Preserve operational I/O failures such as EIO, ESTALE, or EMFILE.
            raise
        raise ImageDecodeError(
            f"Cannot decode supported image file: {image_path}"
        ) from exc
    finally:
        if source is not None:
            source.close()
