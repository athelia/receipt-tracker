"""Does the crop find the receipt in a photo, and keep the full image when it cannot?

Photos are synthetic: a rendered receipt on a dark, noisy, unevenly lit table, so
the true paper box is known exactly.
"""

import pytest
from PIL import Image, ImageDraw, ImageFont

import synthetic
from receipt_tracker.extractors import images

RECEIPT = synthetic.render(synthetic.sample_truth())


def contains(outer, inner, slack=0.0):
    """Does `outer` hold `inner`? Clipping the paper would lose text, so no slack by default."""
    pad_x, pad_y = (inner[2] - inner[0]) * slack, (inner[3] - inner[1]) * slack
    return (
        outer[0] <= inner[0] + pad_x
        and outer[1] <= inner[1] + pad_y
        and outer[2] >= inner[2] - pad_x
        and outer[3] >= inner[3] - pad_y
    )


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


PLACEMENTS = {
    "centred": dict(),
    "small in frame": dict(receipt_width=0.3),
    "off to one side": dict(centre=(0.3, 0.55)),
    "wide frame": dict(frame=(1600, 1200), receipt_width=0.25),
    "tilted 7 degrees": dict(angle=7),
    "tilted the other way": dict(angle=-10),
    "tilted and off centre": dict(angle=12, centre=(0.6, 0.4), receipt_width=0.4),
    "dim table": dict(table=30, lighting=20),
    "bright table": dict(table=110, lighting=30),
    "white table": dict(table=225, lighting=20, noise=6.0),
    "white table, nearly the paper colour": dict(table=240, lighting=10, noise=4.0),
    "white table with a coarse speckle": dict(table=225, lighting=20, noise=12.0),
    "white table, extreme sensor noise": dict(table=225, lighting=20, noise=90.0),
    "heavy noise": dict(noise=25.0),
}


@pytest.mark.parametrize("options", PLACEMENTS.values(), ids=PLACEMENTS.keys())
def test_the_receipt_is_found_and_kept_whole(options):
    photo, paper, ink = synthetic.on_table(RECEIPT, **options)

    crop = images.crop_to_receipt(photo)

    assert crop.reason == "cropped", crop.reason
    assert contains(crop.box, ink), (crop.box, ink)  # the text is never clipped
    assert area(crop.box) <= area(paper) * 1.9  # and not much background kept
    assert crop.image.size == (crop.box[2] - crop.box[0], crop.box[3] - crop.box[1])
    assert crop.fraction == pytest.approx(area(crop.box) / (photo.width * photo.height), abs=0.01)


def uniform(colour, size=(600, 800)):
    return Image.new("RGB", size, colour)


CANNOT_FIND = {
    "all white (a white desk, nothing on it)": uniform((250, 250, 250)),
    "all dark": uniform((20, 20, 20)),
    "flat grey": uniform((128, 128, 128)),
}


@pytest.mark.parametrize("image", CANNOT_FIND.values(), ids=CANNOT_FIND.keys())
def test_with_no_text_to_find_the_full_image_comes_back(image):
    crop = images.crop_to_receipt(image)
    assert crop.box is None
    assert crop.reason in ("no text found", "box too small")
    assert crop.image is image and crop.fraction == 1.0


@pytest.mark.parametrize("width", [640, 1100], ids=["normal", "prices far from descriptions"])
def test_a_receipt_filling_the_frame_is_still_cropped_never_cut(width):
    # A narrow column of prices carries far less ink than the descriptions beside it, and
    # must not be taken for a stray block however far from them it sits.
    receipt = synthetic.render(synthetic.sample_truth(), width=width)
    crop = images.crop_to_receipt(receipt)
    assert crop.reason == "cropped"
    assert crop.box[0] <= 20 and crop.box[2] >= width - 22  # all of the width, prices included
    assert crop.box[1] <= 20 and crop.box[3] >= receipt.height - 20  # and the footer


def test_a_white_table_is_cropped_tightly_even_when_it_matches_the_paper():
    # Containment is covered by PLACEMENTS; the point here is that the crop is not the frame.
    photo, *_ = synthetic.on_table(RECEIPT, table=240, lighting=10, noise=4.0)
    crop = images.crop_to_receipt(photo)
    assert crop.reason == "cropped"
    assert crop.box[2] - crop.box[0] < photo.width * 0.7


def test_a_box_too_small_to_be_a_receipt_is_not_trusted(monkeypatch):
    photo, *_ = synthetic.on_table(RECEIPT, receipt_width=0.3)  # about a tenth of the frame
    monkeypatch.setattr(images, "MIN_CROP_FRACTION", 0.5)
    crop = images.crop_to_receipt(photo)
    assert crop.box is None and "too small" in crop.reason


def test_a_sideways_photo_is_cropped_after_it_is_turned_upright():
    photo, *_ = synthetic.on_table(RECEIPT.rotate(90, expand=True), frame=(1600, 1200))
    assert photo.width > photo.height  # a sideways photo
    first, _ = images.orientations(photo)
    crop = images.crop_to_receipt(first)
    assert crop.reason == "cropped"
    assert crop.image.height > crop.image.width  # an upright receipt


def test_the_crop_does_not_change_the_source_image():
    photo, *_ = synthetic.on_table(RECEIPT)
    before = photo.copy()
    images.crop_to_receipt(photo)
    assert photo.tobytes() == before.tobytes()


DISTRACTORS = {
    "bright object above the receipt": (0.05, 0.04, 0.30, 0.10),
    "bright object beside the receipt": (0.80, 0.42, 0.95, 0.52),
}


@pytest.mark.parametrize("where", DISTRACTORS.values(), ids=DISTRACTORS.keys())
def test_other_objects_in_the_frame_never_cause_the_text_to_be_cut(where):
    photo, _, ink = synthetic.on_table(RECEIPT, receipt_width=0.5)
    left, top, right, bottom = where
    photo.paste(
        (235, 235, 235),
        (
            int(photo.width * left),
            int(photo.height * top),
            int(photo.width * right),
            int(photo.height * bottom),
        ),
    )
    crop = images.crop_to_receipt(photo)
    assert crop.reason == "cropped"
    assert contains(crop.box, ink)  # the box may grow to take in the object, but never shrinks


def test_the_box_found_on_the_small_copy_is_scaled_up_and_given_a_margin():
    # Found at (100, 200)-(300, 600) on a 500x1000 copy of a 1000x2000 photo.
    box = images._scaled_box((1000, 2000), (100, 200, 300, 600), (500, 1000))
    left, top, right, bottom = 200, 400, 600, 1200  # the same box at full size
    margin_x = (right - left) * images.MARGIN_X
    margin_y = (bottom - top) * images.MARGIN_Y
    assert box == pytest.approx(
        (left - margin_x, top - margin_y, right + margin_x, bottom + margin_y), abs=2
    )


def test_the_margin_never_runs_off_the_photo():
    assert images._scaled_box((1000, 2000), (0, 0, 500, 1000), (500, 1000)) == (0, 0, 1000, 2000)


def clutter_box(photo, box):
    return synthetic.add_print(photo, box, lines=int((box[3] - box[1]) * photo.height / 26))


def test_a_faint_stray_label_far_from_the_receipt_does_not_stretch_the_crop():
    photo, _, ink = synthetic.on_table(RECEIPT, receipt_width=0.4, centre=(0.4, 0.5))
    synthetic.add_print(photo, (0.84, 0.05, 1.0, 0.07), lines=1)  # one short line, far right
    crop = images.crop_to_receipt(photo)
    assert contains(crop.box, ink)
    assert crop.box[2] < photo.width * 0.80  # the label is left out


def test_a_rival_block_of_print_with_real_weight_is_kept_too():
    photo, _, ink = synthetic.on_table(RECEIPT, receipt_width=0.35, centre=(0.3, 0.5))
    other = clutter_box(photo, (0.72, 0.30, 0.95, 0.70))  # about as much print as the receipt
    crop = images.crop_to_receipt(photo)
    assert contains(crop.box, ink) and contains(crop.box, other)  # errs large, never cuts


def with_sparse_footer(body):
    """The receipt with a gap and then one short word, small, below it."""
    gap, footer_height = body.height // 12, 70
    page = Image.new("RGB", (body.width, body.height + gap + footer_height), "white")
    page.paste(body, (0, 0))
    font = ImageFont.load_default(size=12)
    draw = ImageDraw.Draw(page)
    draw.text((body.width // 3, body.height + gap), "THANKS", fill="black", font=font)
    return page


def test_a_sparse_footer_below_the_body_is_kept():
    # Under 0.5% of a row is ink: it would be dropped if rows were judged as strictly as columns.
    page = with_sparse_footer(RECEIPT)
    crop = images.crop_to_receipt(page)
    assert crop.reason == "cropped"
    assert crop.box[3] >= page.height - 20  # the footer's line is inside the box


def dense_body_then_sparse_footer():
    """A row profile (0-255, ink per row): body at ~8% ink, a big gap, then a ~1% footer."""
    return [0] * 100 + [20] * 700 + [0] * 350 + [2] * 50 + [0] * 100


def test_judged_strictly_a_sparse_far_footer_is_a_faint_stray_block_and_is_dropped():
    first, last = images._span(dense_body_then_sparse_footer(), strict=True)
    assert last < 820  # the footer (rows 1150-1200) is both faint and far


FOOTERS = {
    "sparse, after a big gap": (dense_body_then_sparse_footer(), 1195),
    "very faint, below the column floor": ([0] * 100 + [20] * 700 + [0] * 100 + [1, 0] * 25, 940),
}


@pytest.mark.parametrize("profile, last_row", FOOTERS.values(), ids=FOOTERS.keys())
def test_judged_leniently_a_footer_is_kept_however_sparse_or_faint(profile, last_row):
    first, last = images._span(profile, strict=False)
    assert first <= 105 and last >= last_row


def test_rows_are_judged_leniently_and_columns_strictly(monkeypatch):
    # Rows: a sparse footer must survive. Columns: faint tails beside the receipt must not.
    modes = []
    original = images._span

    def spy(profile, strict):
        modes.append((len(profile), strict))
        return original(profile, strict=strict)

    monkeypatch.setattr(images, "_span", spy)
    photo, *_ = synthetic.on_table(RECEIPT)
    images.crop_to_receipt(photo)
    (rows_length, rows_mode), (columns_length, columns_mode) = modes
    assert rows_length > columns_length  # the photo is taller than wide
    assert (rows_mode, columns_mode) == (False, True)


def test_judged_strictly_a_trace_of_ink_is_not_print():
    # About 0.2% ink per line: below the column floor (0.5%), though a footer may be this faint.
    assert images._span([1, 0] * 500, strict=True) is None
