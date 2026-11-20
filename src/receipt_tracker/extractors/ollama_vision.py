"""Vision backend: a local vision model served by Ollama over the LAN.

Photos are decoded, oriented and downsized in-process (see `images`), then sent
to the Ollama server named by `model_server_url` with a JSON schema, so the
model must answer in a fixed shape. Anything that does not parse into a valid
receipt is an `ExtractionError`: it is never saved as data.

Images only go to a loopback or private-network address. A server on a public
address is refused before any image is sent.
"""

import base64
import contextvars
import io
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image

from receipt_tracker.config import Settings
from receipt_tracker.extractors import images
from receipt_tracker.extractors.base import (
    BackendUnavailable,
    ExtractedHeader,
    ExtractedLine,
    ExtractedReceipt,
    ExtractionError,
    ExtractionResult,
)
from receipt_tracker.logging_setup import event

PROMPT = """\
You are reading a photographed grocery receipt. Transcribe what is printed; do \
not guess or infer anything that is not visible. Use null for any field you \
cannot read.

- store: the store name as printed at the top.
- purchased_at: the purchase date as YYYY-MM-DD, or YYYY-MM-DDTHH:MM if a time is printed.
- receipt_number: the receipt, transaction or invoice number as printed.
- subtotal and total: amounts as printed, with a decimal point, e.g. "12.34".
- payment_method: how it was paid (e.g. "VISA", "CASH", "DEBIT").
- you_saved: the receipt's total savings printed near the bottom ("you saved", \
"total savings"), as a positive amount, or null if the receipt has none. Not \
the per-item notes.
- lines: one entry per purchased item, in printed order. Include item_code if \
the line prints a product code. raw_text is the item description exactly as \
printed. line_price is only the number printed for that line, e.g. "5.59", \
with no tax code or letters after it (negative for a printed discount or \
refund). quantity and unit_price are bare numbers; put the unit (e.g. "lb", \
"kg", "ea") in unit. Fill them only if the line prints them. discount_percent only for a printed \
percentage discount.
- The price printed on an item's line is already after any discount. A line \
printed under an item that only reports a discount (e.g. "20% off", "You \
saved: $0.95") is a note, not an item: do not list it and do not change the \
item's price. If the note states a percentage, set discount_percent on the \
item above it.
- item_code: the product code is the run of digits printed at the start of \
the line, or in a column beside the description. Copy the digits exactly as \
printed, one digit at a time, into item_code and leave them out of raw_text. \
Use null if the line prints no code.
- Do not include totals, tax, payment or change lines in `lines`.
- If the same item is printed on two lines, list it twice. Never merge or skip \
lines.
"""

MULTI_IMAGE_NOTE = """
The images are consecutive parts of one receipt, and neighbouring images may \
overlap: a line that appears at the end of one image and the start of the next \
is one printed line, so list it once.
"""

_NULLABLE_STRING = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "store": _NULLABLE_STRING,
        "purchased_at": _NULLABLE_STRING,
        "receipt_number": _NULLABLE_STRING,
        "subtotal": _NULLABLE_STRING,
        "total": _NULLABLE_STRING,
        "payment_method": _NULLABLE_STRING,
        "you_saved": _NULLABLE_STRING,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_code": _NULLABLE_STRING,
                    "raw_text": {"type": "string"},
                    "quantity": _NULLABLE_STRING,
                    "unit": _NULLABLE_STRING,
                    "unit_price": _NULLABLE_STRING,
                    "discount_percent": _NULLABLE_STRING,
                    "line_price": {"type": "string"},
                },
                "required": ["item_code", "raw_text", "line_price"],
            },
        },
    },
    "required": ["lines"],
}


def _decimal(value: object, field: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ExtractionError(f"{field}: expected a number, got {value!r}")
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ExtractionError(f"{field}: not a number: {value!r}") from None
    if not number.is_finite():
        raise ExtractionError(f"{field}: not a finite number: {value!r}")
    return number


# Receipts print tax codes after a price ("5.59 E") and some print a trailing
# minus for credits ("2.00-"). The flag is dropped; tax is a v2 concern.
_PRICE = re.compile(r"^\s*(-)?\s*\$?\s*([\d,]*\.?\d+)\s*[A-Za-z]{0,2}\s*(-)?\s*$")


def _price(value: object) -> object:
    if isinstance(value, str):
        match = _PRICE.match(value)
        if match:
            sign = "-" if (match[1] or match[3]) else ""
            return sign + match[2]
    return value


# Quantity and unit price are sometimes copied with their unit: "0.34 lb",
# "6.99 / EA". The number is kept and the unit is used if the line has none.
_WITH_UNIT = re.compile(r"^\s*\$?\s*([\d,]*\.?\d+)\s*(?:/|per\b|@)?\s*([A-Za-z]{1,10})?\s*$")


def _number_and_unit(value: object) -> tuple[object, str | None]:
    if isinstance(value, str):
        match = _WITH_UNIT.match(value)
        if match:
            return match[1], match[2]
    return value, None


def _cents(value: object, field: str) -> int | None:
    number = _decimal(_price(value), field)
    if number is None:
        return None
    cents = number * 100
    if cents != cents.to_integral_value():
        raise ExtractionError(f"{field}: more than two decimal places: {value!r}")
    return int(cents)


def _scaled(value: object, field: str, factor: int) -> int | None:
    number = _decimal(value, field)
    return None if number is None else int((number * factor).to_integral_value())


def _text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExtractionError(f"{field}: expected text, got {value!r}")
    return value.strip() or None


def _discount_bp(value: object, index: int) -> int | None:
    basis_points = _scaled(value, f"line {index} discount_percent", 100)
    # A printed "-20%" is still a 20% discount.
    return None if basis_points is None else abs(basis_points)


def _savings(value: object) -> int | None:
    cents = _cents(value, "you_saved")
    return None if cents is None else abs(cents)


def _parse_line(index: int, raw: object) -> ExtractedLine:
    if not isinstance(raw, dict):
        raise ExtractionError(f"line {index}: not an object")
    raw_text = _text(raw.get("raw_text"), f"line {index} raw_text")
    price = _cents(raw.get("line_price"), f"line {index} line_price")
    if raw_text is None or price is None:
        raise ExtractionError(f"line {index}: missing description or price")
    quantity, quantity_unit = _number_and_unit(raw.get("quantity"))
    unit_price, price_unit = _number_and_unit(raw.get("unit_price"))
    unit = _text(raw.get("unit"), f"line {index} unit") or quantity_unit or price_unit
    if quantity is None and unit_price is None:
        unit = None  # a unit only means something next to a quantity or unit price
    return ExtractedLine(
        raw_text=raw_text,
        line_price_cents=price,
        item_code=_text(raw.get("item_code"), f"line {index} item_code"),
        quantity_milli=_scaled(quantity, f"line {index} quantity", 1000),
        unit=unit,
        unit_price_cents=_cents(unit_price, f"line {index} unit_price"),
        discount_bp=_discount_bp(raw.get("discount_percent"), index),
    )


def parse_receipt(content: str) -> ExtractedReceipt:
    """Turn the model's JSON answer into a receipt, or raise `ExtractionError`."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ExtractionError(f"Model output is not valid JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        raise ExtractionError("Model output has no 'lines' list")
    if not data["lines"]:
        raise ExtractionError("Model found no line items")
    try:
        header = ExtractedHeader(
            store_name=_text(data.get("store"), "store"),
            purchased_at=_text(data.get("purchased_at"), "purchased_at"),
            receipt_number=_text(data.get("receipt_number"), "receipt_number"),
            subtotal_cents=_cents(data.get("subtotal"), "subtotal"),
            total_cents=_cents(data.get("total"), "total"),
            payment_method=_text(data.get("payment_method"), "payment_method"),
            savings_cents=_savings(data.get("you_saved")),
        )
        lines = []
        problems = []
        for index, raw in enumerate(data["lines"], start=1):
            try:
                lines.append(_parse_line(index, raw))
            except (ExtractionError, ValueError) as error:
                problems.append(str(error))
        if problems:
            shown = "; ".join(problems[:8])
            more = f" (+{len(problems) - 8} more)" if len(problems) > 8 else ""
            raise ExtractionError(f"{len(problems)} bad line(s): {shown}{more}")
    except ValueError as error:  # from the dataclass validators
        raise ExtractionError(f"Invalid receipt data: {error}") from error
    return ExtractedReceipt(header=header, lines=tuple(lines))


def require_local(base_url: str) -> None:
    """Refuse to send images to anything but a loopback or private-network host."""
    host = urllib.parse.urlparse(base_url).hostname
    if not host:
        raise ExtractionError(f"Invalid model server URL: {base_url!r}")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as error:
        raise BackendUnavailable(f"Cannot resolve model server {host!r}: {error}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            raise ExtractionError(
                f"Refusing to send images to {host} ({ip}): not on the local network"
            )


class OllamaVisionExtractor:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 300.0,
        max_edge: int = 2240,
        num_ctx: int = 8192,
        crop: bool = True,
        progress_seconds: float = 30.0,
        max_aspect: float = images.MAX_ASPECT,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_edge = max_edge
        self.num_ctx = num_ctx
        self.crop = crop
        self.progress_seconds = progress_seconds
        self.max_aspect = max_aspect
        self._version: str | None = None
        self.last_raw: str | None = None  # latest model answer, for debugging

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            if error.code == 404:
                raise BackendUnavailable(
                    f"Model {self.model!r} not found on the server (ollama pull it): {detail}"
                ) from error
            if error.code >= 500:
                raise BackendUnavailable(f"Model server error {error.code}: {detail}") from error
            raise ExtractionError(
                f"Model server rejected the request ({error.code}): {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            raise BackendUnavailable(f"Cannot reach model server: {error}") from error
        except json.JSONDecodeError as error:
            raise ExtractionError(f"Model server returned invalid JSON: {error}") from error

    def model_version(self) -> str:
        """The installed model's digest, so a re-pulled model shows as a new version."""
        if self._version is None:
            wanted = {self.model, f"{self.model}:latest"}
            for entry in self._call("GET", "/api/tags").get("models", []):
                if entry.get("name") in wanted:
                    self._version = str(entry.get("digest", ""))[:12] or "unknown"
                    break
            else:
                raise BackendUnavailable(f"Model {self.model!r} is not installed (ollama pull it)")
        return self._version

    def _ask(self, jpegs: list[bytes], attempt: int = 1, of: int = 1) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {
                    "role": "user",
                    "content": PROMPT + (MULTI_IMAGE_NOTE if len(jpegs) > 1 else ""),
                    "images": [base64.b64encode(jpeg).decode() for jpeg in jpegs],
                }
            ],
        }
        sizes = [list(Image.open(io.BytesIO(jpeg)).size) for jpeg in jpegs]
        kb = round(sum(len(jpeg) for jpeg in jpegs) / 1024)
        event(
            "model_call_started",
            attempt=attempt,
            of=of,
            model=self.model,
            images=len(jpegs),
            image_sizes=sizes,
            image_kb=kb,
            num_ctx=self.num_ctx,
            timeout_s=self.timeout,
        )
        started = time.monotonic()
        with self._ticker(attempt, started):
            reply = self._call("POST", "/api/chat", body)
        content = (reply.get("message") or {}).get("content")
        event(
            "model_call",
            attempt=attempt,
            model=self.model,
            images=len(jpegs),
            image_sizes=sizes,
            image_kb=kb,
            num_ctx=self.num_ctx,
            duration_s=round(time.monotonic() - started, 2),
            ollama_total_s=_seconds(reply.get("total_duration")),
            ollama_load_s=_seconds(reply.get("load_duration")),
            prompt_tokens=reply.get("prompt_eval_count"),
            output_tokens=reply.get("eval_count"),
            answer_chars=len(content) if isinstance(content, str) else 0,
        )
        if not isinstance(content, str) or not content.strip():
            raise ExtractionError("Model returned an empty answer")
        self.last_raw = content
        return content

    @contextmanager
    def _ticker(self, attempt: int, started: float) -> Iterator[None]:
        """While the model works, say so every `progress_seconds`, so a run is never silent."""
        if self.progress_seconds <= 0:
            yield
            return
        stop = threading.Event()

        def tick() -> None:
            while not stop.wait(self.progress_seconds):
                event(
                    "model_call_waiting",
                    attempt=attempt,
                    waited_s=round(time.monotonic() - started),
                    timeout_s=self.timeout,
                )

        thread = threading.Thread(target=contextvars.copy_context().run, args=(tick,), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def _cropped(self, image: Image.Image) -> Image.Image:
        """Cut the background away when the receipt can be found, and log what happened."""
        if not self.crop:
            return image
        result = images.crop_to_receipt(image)
        event(
            "image_cropped" if result.box else "image_not_cropped",
            reason=result.reason,
            fraction=round(result.fraction, 2),
            box=list(result.box) if result.box else None,
            size=list(image.size),
        )
        return result.image

    def extract(self, photos: list[Path]) -> ExtractionResult:
        if not photos:
            raise ExtractionError("No photos to read")
        require_local(self.base_url)
        version = self.model_version()
        event(
            "extraction_started",
            photos=len(photos),
            model=self.model,
            model_version=version,
            crop=self.crop,
            max_edge=self.max_edge,
            num_ctx=self.num_ctx,
        )
        variants = []
        for number, photo in enumerate(photos, 1):
            options = list(images.orientations(images.load(photo)))
            variants.append(options)
            event("photo_loaded", photo=number, of=len(photos), size=list(options[0].size))
        last_error: ExtractionError | None = None
        last_answer: str | None = None
        attempts = max(len(v) for v in variants)
        for attempt in range(attempts):
            pieces = []
            for options in variants:
                candidate = self._cropped(options[min(attempt, len(options) - 1)])
                cut = images.tiles(candidate, self.max_aspect)
                if len(cut) > 1:
                    event(
                        "image_tiled",
                        pieces=len(cut),
                        size=list(candidate.size),
                        aspect=round(candidate.height / candidate.width, 2),
                        max_aspect=self.max_aspect,
                    )
                pieces.extend(cut)
            jpegs = [images.to_jpeg(piece, self.max_edge) for piece in pieces]
            content = last_answer = self._ask(jpegs, attempt + 1, attempts)
            try:
                receipt = parse_receipt(content)
            except ExtractionError as error:
                last_error = error
                event(
                    "orientation_failed",
                    level=logging.WARNING,
                    attempt=attempt + 1,
                    of=attempts,
                    left=attempts - attempt - 1,
                    error=str(error),
                )
                continue
            event("orientation_used", attempt=attempt + 1, of=attempts, lines=len(receipt.lines))
            return ExtractionResult(
                receipt=receipt,
                backend="ollama-vision",
                model=self.model,
                model_version=version,
                raw_output=content,
            )
        raise ExtractionError(
            f"No orientation produced a valid receipt: {last_error}", raw_output=last_answer
        )


def _seconds(nanoseconds: object) -> float | None:
    """Ollama reports durations in nanoseconds."""
    if isinstance(nanoseconds, int | float) and not isinstance(nanoseconds, bool):
        return round(nanoseconds / 1e9, 2)
    return None


def build(settings: Settings) -> OllamaVisionExtractor:
    return OllamaVisionExtractor(
        settings.model_server_url,
        settings.vision_model,
        timeout=settings.model_timeout_seconds,
        max_edge=settings.vision_max_edge,
        num_ctx=settings.vision_num_ctx,
        crop=settings.vision_crop,
        progress_seconds=settings.vision_progress_seconds,
        max_aspect=settings.vision_max_aspect,
    )
