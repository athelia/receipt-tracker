"""Exact checks of the image pipeline on synthetic pictures, no model involved.

Marker images have a distinct colour in three corners, so a rotation or flip
shows up as a colour in the wrong place. Row-coded images tell each tile's
vertical offset from its first pixel.
"""

import io

import pytest
from PIL import Image

from receipt_tracker.extractors import images

RED, BLUE, GREEN, WHITE = (255, 0, 0), (0, 0, 255), (0, 160, 0), (255, 255, 255)


def marker_image(width=100, height=200):
    """Upright: red top-left, blue top-right, green bottom-left, white elsewhere."""
    image = Image.new("RGB", (width, height), WHITE)
    for x0, y0, colour in [(0, 0, RED), (width - 10, 0, BLUE), (0, height - 10, GREEN)]:
        image.paste(colour, (x0, y0, x0 + 10, y0 + 10))
    return image


def corners(image):
    """Colours at the four corners: top-left, top-right, bottom-left, bottom-right."""
    w, h = image.size
    return tuple(image.getpixel(p) for p in [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)])


UPRIGHT = (RED, BLUE, GREEN, WHITE)


def test_marker_image_reads_as_upright():
    assert corners(marker_image()) == UPRIGHT


def test_portrait_is_used_as_is_with_no_retry():
    candidates = list(images.orientations(marker_image()))
    assert len(candidates) == 1
    assert corners(candidates[0]) == UPRIGHT


def test_landscape_first_candidate_undoes_a_clockwise_turn():
    on_its_side = marker_image().rotate(-90, expand=True)  # photographed turned clockwise
    assert on_its_side.width > on_its_side.height
    first, second = images.orientations(on_its_side)
    assert corners(first) == UPRIGHT
    assert first.size == (100, 200)
    assert corners(second) == corners(marker_image().rotate(180))  # the other guess


def test_landscape_second_candidate_undoes_a_counter_clockwise_turn():
    on_its_side = marker_image().rotate(90, expand=True)
    _, second = images.orientations(on_its_side)
    assert corners(second) == UPRIGHT


def test_exif_orientation_is_applied_on_load(tmp_path):
    path = tmp_path / "exif.jpg"
    stored = marker_image(100, 200)
    exif = stored.getexif()
    exif[0x0112] = 6  # display rotated 90 degrees clockwise
    stored.save(path, exif=exif, quality=100)

    loaded = images.load(path)

    assert loaded.size == (200, 100)
    # Clockwise turn: top-left (red) goes to top-right, top-right (blue) to bottom-right.
    assert loaded.getpixel((197, 2))[0] > 200 and loaded.getpixel((197, 2))[1] < 60
    assert loaded.getpixel((197, 97))[2] > 200 and loaded.getpixel((197, 97))[0] < 60


def row_coded(width, height):
    """Pixel (x, y) encodes y in its red and green channels."""
    image = Image.new("RGB", (width, height))
    image.putdata([(y % 256, y // 256, 0) for y in range(height) for _ in range(width)])
    return image


def offset_of(tile):
    red, green, _ = tile.getpixel((0, 0))
    return green * 256 + red


def test_short_image_is_one_tile():
    assert len(images.tiles(row_coded(100, 250))) == 1


def test_tall_image_is_cut_into_overlapping_tiles_that_cover_it():
    width, height = 100, 1000
    tiles = images.tiles(row_coded(width, height))
    max_height = int(width * images.MAX_ASPECT)
    offsets = [offset_of(tile) for tile in tiles]

    assert offsets[0] == 0
    assert all(tile.width == width and tile.height <= max_height for tile in tiles)
    assert offsets[-1] + tiles[-1].height == height  # the bottom is included
    for here, after, tile in zip(offsets, offsets[1:], tiles, strict=False):
        assert after < here + tile.height  # neighbours overlap, so no line is cut in two
        overlap = here + tile.height - after
        assert overlap == pytest.approx(max_height * images.TILE_OVERLAP, abs=1)


def test_tile_pixels_match_the_source_rows():
    source = row_coded(100, 1000)
    for tile in images.tiles(source):
        top = offset_of(tile)
        assert tile.getpixel((50, tile.height - 1)) == source.getpixel((50, top + tile.height - 1))


def test_to_jpeg_limits_the_longest_edge_and_keeps_proportions():
    data = images.to_jpeg(marker_image(1000, 2000), max_edge=500)
    decoded = Image.open(io.BytesIO(data))
    assert decoded.format == "JPEG"
    assert decoded.size == (250, 500)


def test_to_jpeg_never_enlarges():
    decoded = Image.open(io.BytesIO(images.to_jpeg(marker_image(100, 200), max_edge=2240)))
    assert decoded.size == (100, 200)


@pytest.mark.parametrize("height", [251, 600, 960, 1000, 1234, 3000])
def test_tiles_cover_every_row_whatever_the_height(height):
    tiles = images.tiles(row_coded(100, height))
    covered = set()
    for tile in tiles:
        top = offset_of(tile)
        covered.update(range(top, top + tile.height))
    assert covered == set(range(height))
