"""Prepare receipt photos for a vision model.

Photos arrive as HEIC (and other formats) with the receipt often sideways in a
landscape frame, and some receipts are much taller than a model can read in
one image. This module decodes, orients, downsizes and, when needed, tiles
photos into JPEG bytes. Everything happens in-process; nothing is sent
anywhere.
"""

import io
from collections.abc import Iterator
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from receipt_tracker.extractors.base import ExtractionError

register_heif_opener()

# A receipt taller than this multiple of its width is cut into overlapping tiles.
MAX_ASPECT = 2.5
TILE_OVERLAP = 0.10


def load(photo: Path) -> Image.Image:
    """Decode a photo and apply its EXIF orientation."""
    try:
        with Image.open(photo) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ExtractionError(f"Cannot read image {photo.name}: {error}") from error
    return image.convert("RGB")


def orientations(image: Image.Image) -> Iterator[Image.Image]:
    """Candidate upright versions of the image, most likely first.

    A portrait photo is tried as is. A landscape photo almost certainly has
    the receipt on its side, so it is tried rotated both ways. The backend
    keeps the first one that yields a usable result.
    """
    if image.height >= image.width:
        yield image
        return
    yield image.rotate(90, expand=True)
    yield image.rotate(-90, expand=True)


def tiles(image: Image.Image) -> list[Image.Image]:
    """Cut a very tall image into overlapping, roughly square-ish pieces."""
    max_height = int(image.width * MAX_ASPECT)
    if image.height <= max_height:
        return [image]
    step = int(max_height * (1 - TILE_OVERLAP))
    pieces, top = [], 0
    while True:
        bottom = min(top + max_height, image.height)
        pieces.append(image.crop((0, top, image.width, bottom)))
        if bottom >= image.height:
            return pieces
        top += step


def to_jpeg(image: Image.Image, max_edge: int, quality: int = 90) -> bytes:
    """Downsize so the longest edge is at most `max_edge`, and encode as JPEG."""
    image = image.copy()
    image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()
