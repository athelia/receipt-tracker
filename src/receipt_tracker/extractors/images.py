"""Prepare receipt photos for a vision model.

Photos arrive as HEIC (and other formats) with the receipt often sideways in a
landscape frame, and some receipts are much taller than a model can read in
one image. This module decodes, orients, downsizes and, when needed, tiles
photos into JPEG bytes. Everything happens in-process; nothing is sent
anywhere.
"""

import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from receipt_tracker.extractors.base import ExtractionError

register_heif_opener()

# A receipt taller than this multiple of its width is cut into overlapping tiles.
MAX_ASPECT = 4.0
TILE_OVERLAP = 0.10

# Finding the receipt by its text. A table can be the same colour as the paper, but it
# has no fine dark detail, so the receipt is wherever the print is.
ANALYSIS_EDGE = 1200  # the search runs on a copy this size, not on the full photo
NOISE_BLUR = 1.0  # softening applied first so sensor noise is not mistaken for print
SURROUND_BLUR = 4.0  # print is what is darker than this blurred neighbourhood
INK_SHARE = 0.5  # ink is darker than its surroundings by this share of the strongest such pixel
MIN_INK_CONTRAST = 20  # the strongest contrast must be at least this (0-255), else no text
LINE_DENSITY = 0.005  # a column holds print when this share of it is ink
FOOTER_DENSITY = 0.0005  # a row holds print at a tenth of that: a footer can be very faint
TAIL_SHARE = 0.15  # print must be at least this share of the typical density in the dense part
SMOOTHING = 0.01  # profiles are averaged over this share of the photo before comparing
CLUSTER_GAP = 0.20  # print closer together than this share of the photo is one block
CLUSTER_MASS = 0.15  # blocks with less than this share of the main block's ink are ignored; a
# narrow price column far from the descriptions carries well under a third of their ink
MARGIN_X = 0.06  # extra border kept around the text, as a share of its width
MARGIN_Y = 0.04  # and of its height
MIN_CROP_FRACTION = 0.06  # a smaller box is probably a speck, not a receipt


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


def tiles(image: Image.Image, max_aspect: float = MAX_ASPECT) -> list[Image.Image]:
    """Cut a very tall image into overlapping pieces no taller than `max_aspect` times the width."""
    max_height = int(image.width * max_aspect)
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


@dataclass(frozen=True)
class Crop:
    """The outcome of looking for the receipt in a photo.

    `box` is in the source image's pixels, or None when the full image was kept;
    `reason` says why ("cropped", or why not).
    """

    image: Image.Image
    box: tuple[int, int, int, int] | None
    reason: str
    fraction: float  # cropped area as a fraction of the source; 1.0 when not cropped


def crop_to_receipt(image: Image.Image) -> Crop:
    """Cut away the background around the receipt so more pixels land on the text.

    Pillow only, no model. The receipt is located by its print, so a white table
    works as well as a dark one. Where the print is unclear the box errs large: it
    keeps background rather than risk cutting text. The full image comes back
    unchanged only when there is no text to find.
    """
    small = image.convert("L")
    scale = ANALYSIS_EDGE / max(image.size)
    if scale < 1:
        small = small.resize((round(image.width * scale), round(image.height * scale)))
    ink = _ink(small)
    if ink is None:
        return Crop(image, None, "no text found", 1.0)

    width, height = ink.size
    # Rows keep all print, from first to last, so a sparse footer is never cut: trimming
    # the top and bottom saves little. Columns are where the background and stray tails
    # are, so they are strict.
    rows = _span(list(ink.resize((1, height), Image.Resampling.BOX).tobytes()), strict=False)
    columns = _span(list(ink.resize((width, 1), Image.Resampling.BOX).tobytes()), strict=True)
    if rows is None or columns is None:
        return Crop(image, None, "no text found", 1.0)

    box = _scaled_box(image.size, (columns[0], rows[0], columns[1] + 1, rows[1] + 1), ink.size)
    fraction = (box[2] - box[0]) * (box[3] - box[1]) / (image.width * image.height)
    if fraction < MIN_CROP_FRACTION:
        return Crop(image, None, "box too small", 1.0)
    return Crop(image.crop(box), box, "cropped", fraction)


def _ink(gray: Image.Image) -> Image.Image | None:
    """A 0/255 map of pixels that are print: much darker than their neighbourhood."""
    softened = gray.filter(ImageFilter.GaussianBlur(NOISE_BLUR))
    surround = gray.filter(ImageFilter.GaussianBlur(SURROUND_BLUR))
    darker = ImageChops.subtract(surround, softened)  # zero wherever it is not darker
    histogram = darker.histogram()
    total = sum(histogram)
    seen, strongest = 0, 0
    for level in range(255, -1, -1):  # the 99.8th percentile, so one speck does not count
        seen += histogram[level]
        if seen >= total * 0.002:
            strongest = level
            break
    if strongest < MIN_INK_CONTRAST:
        return None
    limit = strongest * INK_SHARE
    return darker.point(lambda value: 255 if value > limit else 0)


def _span(profile: Sequence[int], *, strict: bool) -> tuple[int, int] | None:
    """First and last position along a profile that holds the receipt's print.

    Lenient (`strict=False`): even a very faint trace counts and the span runs from the
    first to the last of it. A sparse footer after a big blank gap is kept.

    Strict: print has to stand out against the photo's own dense part, so a faint tail
    beside the receipt (a stray label, a patterned edge) is ignored. What is left is
    grouped into blocks, and far blocks carrying only a trace of the main block's ink
    are dropped. A rival with real weight is kept, so when unsure the box grows to
    cover both and never cuts the receipt.
    """
    length = len(profile)
    window = max(1, round(length * SMOOTHING))
    prefix = [0]
    for value in profile:
        prefix.append(prefix[-1] + value)
    smooth = [
        (prefix[min(length, i + window + 1)] - prefix[max(0, i - window)])
        / (min(length, i + window + 1) - max(0, i - window))
        for i in range(length)
    ]
    floor = (LINE_DENSITY if strict else FOOTER_DENSITY) * 255
    active = sorted(value for value in smooth if value >= floor)
    if not active:
        return None
    if not strict:
        held = [index for index, value in enumerate(smooth) if value >= floor]
        return held[0], held[-1]
    limit = max(floor, TAIL_SHARE * active[int(len(active) * 0.75)])
    gap = max(1, round(length * CLUSTER_GAP))
    blocks: list[list[float]] = []  # [first, last, ink]
    for index, value in enumerate(smooth):
        if value < limit:
            continue
        if blocks and index - blocks[-1][1] <= gap:
            blocks[-1][1] = index
            blocks[-1][2] += value
        else:
            blocks.append([index, index, value])
    heaviest = max(block[2] for block in blocks)
    kept = [block for block in blocks if block[2] >= heaviest * CLUSTER_MASS]
    return int(kept[0][0]), int(kept[-1][1])


def _scaled_box(
    size: tuple[int, int], box: tuple[int, int, int, int], analysed: tuple[int, int]
) -> tuple[int, int, int, int]:
    """Map a box found on the small analysis copy back to full size, with a margin."""
    sx, sy = size[0] / analysed[0], size[1] / analysed[1]
    left, top, right, bottom = box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy
    pad_x, pad_y = (right - left) * MARGIN_X, (bottom - top) * MARGIN_Y
    return (
        max(0, int(left - pad_x)),
        max(0, int(top - pad_y)),
        min(size[0], int(right + pad_x) + 1),
        min(size[1], int(bottom + pad_y) + 1),
    )
