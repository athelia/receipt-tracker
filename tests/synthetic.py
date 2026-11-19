"""Render a known, made-up receipt to an image, with its ground truth.

Used to test the image pipeline exactly and, opt-in, a real vision model against
text we know. Nothing here is real household data, so it can live in the repo.
The receipt deliberately includes the cases that have tripped models up:
identical neighbouring lines, a percentage discount printed under its item, and
a trailing tax flag.
"""

from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont


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
