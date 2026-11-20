"""Render a known, made-up receipt to an image, with its ground truth.

Used to test the image pipeline exactly and, opt-in, a real vision model against
text we know. Nothing here is real household data, so it can live in the repo.
The receipt deliberately includes the cases that have tripped models up:
identical neighbouring lines, a percentage discount printed under its item, and
a trailing tax flag.
"""

from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw, ImageFont


@dataclass(frozen=True)
class TruthLine:
    code: str
    text: str
    price_cents: int  # the price printed on the line; stores print it already net of discounts
    tax_flag: str = ""
    discount_bp: int | None = None  # a "20% off (20%)   (2.20)" note under the item
    discount_cents: int = 0  # without discount_bp: a "You saved: $0.95" note under the item


@dataclass(frozen=True)
class Truth:
    store: str = "SAMPLE MARKET"
    date: str = "2026-03-14"
    time: str = "10:42"
    number: str = "1042"
    payment: str = "VISA"
    lines: list[TruthLine] = field(default_factory=list)
    tax_cents: int = 136

    @property
    def subtotal_cents(self) -> int:
        return sum(line.price_cents for line in self.lines)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.tax_cents

    @property
    def savings_cents(self) -> int:
        return sum(line.discount_cents for line in self.lines)


def sample_truth() -> Truth:
    return Truth(
        lines=[
            TruthLine("4011", "BANANAS ORGANIC", 189),
            TruthLine("82600012345", "OAT MILK 64OZ", 449, tax_flag="T"),
            TruthLine("82600012345", "OAT MILK 64OZ", 449, tax_flag="T"),
            TruthLine("9012", "COFFEE BEANS 12OZ", 879, discount_bp=2000, discount_cents=220),
            TruthLine("2210", "DICE TOMATO FR 14OZ", 94, discount_cents=95),
            TruthLine("1583", "PEANUT BUTTER 16OZ", 379),
            TruthLine("1583", "PEANUT BUTTER 16OZ", 379),
        ]
    )


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def render(truth: Truth, *, width: int = 640, font_size: int = 22) -> Image.Image:
    """Draw the receipt upright on white, long and narrow like a real one."""
    font = ImageFont.load_default(size=font_size)
    height = font_size * 2
    pad = font_size
    rows: list[tuple[str, str]] = [
        (truth.store, ""),
        (f"{truth.date} {truth.time}", ""),
        (f"RECEIPT {truth.number}", ""),
        ("", ""),
    ]
    for line in truth.lines:
        price = f"{money(line.price_cents)} {line.tax_flag}".strip()
        rows.append((f"{line.code}  {line.text}", price))
        if line.discount_bp:
            percent = line.discount_bp // 100
            rows.append((f"  {percent}% off ({percent}%)", f"({money(line.discount_cents)})"))
        elif line.discount_cents:
            rows.append((f"  You saved: ${money(line.discount_cents)}", ""))
    rows += [
        ("", ""),
        ("SUBTOTAL", money(truth.subtotal_cents)),
        ("TAX", money(truth.tax_cents)),
        ("TOTAL", money(truth.total_cents)),
        (truth.payment, money(truth.total_cents)),
        ("", ""),
        ("YOU SAVED", money(truth.savings_cents)),
    ]
    image = Image.new("RGB", (width, pad * 2 + height * len(rows)), "white")
    draw = ImageDraw.Draw(image)
    for index, (left, right) in enumerate(rows):
        y = pad + index * height
        draw.text((pad, y), left, fill="black", font=font)
        if right:
            draw.text((width - pad - draw.textlength(right, font=font), y), right, "black", font)
    return image


def on_table(
    receipt: Image.Image,
    *,
    frame: tuple[int, int] = (1200, 1600),
    receipt_width: float = 0.5,
    centre: tuple[float, float] = (0.5, 0.5),
    angle: float = 0.0,
    table: int = 70,
    lighting: int = 30,
    noise: float = 8.0,
) -> tuple[Image.Image, tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Paste the receipt onto a dark, noisy, unevenly lit background, like a photo.

    Returns the photo, the box that tightly holds the paper, and the box that
    tightly holds the printed text. A crop may trim empty paper but never the text.
    """
    width, height = frame
    paper = receipt.convert("RGBA")
    scale = width * receipt_width / paper.width
    paper = paper.resize((round(paper.width * scale), round(paper.height * scale)))
    if angle:
        paper = paper.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    left = round(width * centre[0] - paper.width / 2)
    top = round(height * centre[1] - paper.height / 2)

    ramp = Image.linear_gradient("L").resize((width, height))  # darker top, brighter bottom
    shade = ramp.point(lambda value: table + lighting * value // 255)
    shade = ImageChops.add(shade, Image.effect_noise((width, height), noise), offset=-128)
    photo = shade.convert("RGB")
    photo.paste(paper, (left, top), paper)
    alpha = paper.getchannel("A")
    ink = paper.convert("L").point(lambda value: 255 if value < 128 else 0)
    ink = ImageChops.multiply(ink, alpha.point(lambda value: 255 if value > 200 else 0))
    boxes = []
    for mask in (alpha, ink):
        bbox = mask.getbbox()
        assert bbox is not None
        boxes.append((left + bbox[0], top + bbox[1], left + bbox[2], top + bbox[3]))
    return photo, boxes[0], boxes[1]


def add_print(
    photo: Image.Image, box: tuple[float, float, float, float], lines: int, size: int = 22
) -> tuple[int, int, int, int]:
    """Draw `lines` rows of dense text inside a box (as fractions of the photo): a stray label,
    another receipt, a magazine. Returns the pixel box that holds it."""
    font = ImageFont.load_default(size=size)
    left, top = int(photo.width * box[0]), int(photo.height * box[1])
    right, bottom = int(photo.width * box[2]), int(photo.height * box[3])
    draw = ImageDraw.Draw(photo)
    step = max(size + 4, (bottom - top) // max(1, lines))
    for row in range(lines):
        draw.text((left, top + row * step), "ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789", "white", font)
    return left, top, right, min(bottom, top + lines * step)
