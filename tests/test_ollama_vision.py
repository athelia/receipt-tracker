import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image
from pillow_heif import register_heif_opener

from receipt_tracker.config import Settings
from receipt_tracker.extractors import (
    BackendUnavailable,
    ExtractionError,
    available,
    create,
    images,
)
from receipt_tracker.extractors.ollama_vision import (
    OllamaVisionExtractor,
    parse_receipt,
    require_local,
)

register_heif_opener()

GOOD = {
    "store": "Test Mart",
    "purchased_at": "2026-01-02T10:30",
    "receipt_number": "R123",
    "subtotal": "5.00",
    "total": "5.40",
    "payment_method": "VISA",
    "you_saved": "$1.20",
    "lines": [
        {"item_code": "111", "raw_text": "MILK", "line_price": "3.50"},
        {
            "raw_text": "APPLES",
            "quantity": "0.34",
            "unit": "lb",
            "unit_price": "4.41",
            "discount_percent": "20",
            "line_price": "1.50",
        },
    ],
}


class FakeOllama:
    """A real HTTP server on loopback that plays back scripted /api/chat answers."""

    def __init__(self, answers, models=None, chat_status=200):
        self.answers = list(answers)
        self.models = (
            models if models is not None else [{"name": "vis:7b", "digest": "abcdef1234567890"}]
        )
        self.chat_status = chat_status
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"models": outer.models})

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(length)))
                if outer.chat_status != 200:
                    return self._send(outer.chat_status, {"error": "model 'vis:7b' not found"})
                answer = outer.answers.pop(0)
                content = answer if isinstance(answer, str) else json.dumps(answer)
                self._send(200, {"message": {"role": "assistant", "content": content}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve():
    servers = []

    def start(*args, **kwargs):
        servers.append(FakeOllama(*args, **kwargs))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def make_photo(tmp_path, size=(200, 400), name="p.jpg"):
    path = tmp_path / name
    Image.new("RGB", size, "white").save(path)
    return path


def extractor(server, **kwargs):
    return OllamaVisionExtractor(server.url, "vis:7b", timeout=5, **kwargs)


def test_parse_converts_to_integer_units():
    receipt = parse_receipt(json.dumps(GOOD))
    assert (receipt.header.store_name, receipt.header.total_cents) == ("Test Mart", 540)
    assert receipt.header.savings_cents == 120
    milk, apples = receipt.lines
    assert (milk.item_code, milk.line_price_cents) == ("111", 350)
    assert (apples.quantity_milli, apples.unit_price_cents, apples.discount_bp) == (340, 441, 2000)


@pytest.mark.parametrize(
    ("printed", "cents"), [("5.59 E", 559), ("5.59T", 559), ("2.00-", -200), ("-2.00", -200)]
)
def test_parse_drops_tax_flags_and_reads_trailing_minus(printed, cents):
    data = {"lines": [{"raw_text": "X", "line_price": printed}]}
    assert parse_receipt(json.dumps(data)).lines[0].line_price_cents == cents


def test_parse_splits_unit_off_quantity_and_unit_price():
    line = {"raw_text": "X", "line_price": "6.99", "unit_price": "6.99 / EA", "quantity": "2"}
    parsed = parse_receipt(json.dumps({"lines": [line]})).lines[0]
    assert (parsed.unit_price_cents, parsed.quantity_milli, parsed.unit) == (699, 2000, "EA")
    line = {"raw_text": "X", "line_price": "1.50", "quantity": "0.34 lb", "unit": "kg"}
    parsed = parse_receipt(json.dumps({"lines": [line]})).lines[0]
    assert (parsed.quantity_milli, parsed.unit) == (340, "kg")  # an explicit unit wins


@pytest.mark.parametrize("printed", ["20%", "20", "-20%", " 20 % "])
def test_parse_reads_percent_discounts(printed):
    line = {"raw_text": "X", "line_price": "1.00", "discount_percent": printed}
    assert parse_receipt(json.dumps({"lines": [line]})).lines[0].discount_bp == 2000


def test_parse_accepts_dollar_signs_and_numbers():
    data = {"total": "$1,005.00", "lines": [{"raw_text": "X", "line_price": 2.5}]}
    receipt = parse_receipt(json.dumps(data))
    assert (receipt.header.total_cents, receipt.lines[0].line_price_cents) == (100500, 250)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"lines": []}),
        json.dumps({"lines": "none"}),
        json.dumps({"lines": [{"raw_text": "X"}]}),
        json.dumps({"lines": [{"raw_text": "X", "line_price": "abc"}]}),
        json.dumps({"lines": [{"raw_text": "X", "line_price": "1.005"}]}),
        json.dumps({"lines": [{"raw_text": "", "line_price": "1.00"}]}),
        json.dumps({"lines": [{"raw_text": "X", "line_price": "1.00", "discount_percent": "150"}]}),
        json.dumps({"total": "NaN", "lines": [{"raw_text": "X", "line_price": "1.00"}]}),
        json.dumps({"store": 5, "lines": [{"raw_text": "X", "line_price": "1.00"}]}),
    ],
)
def test_malformed_output_is_an_extraction_error(content):
    with pytest.raises(ExtractionError):
        parse_receipt(content)


def test_every_bad_line_is_reported_at_once():
    data = {
        "lines": [{"raw_text": "A", "line_price": "x"}, {"raw_text": "B", "line_price": "1.00"}]
        + [{"raw_text": "C", "line_price": "y"}]
    }
    with pytest.raises(ExtractionError, match=r"2 bad line\(s\).*line 1.*line 3"):
        parse_receipt(json.dumps(data))


def test_extract_returns_result_with_provenance(serve, tmp_path):
    server = serve([GOOD])
    result = extractor(server).extract([make_photo(tmp_path)])
    assert (result.backend, result.model, result.model_version) == (
        "ollama-vision",
        "vis:7b",
        "abcdef123456",
    )
    assert json.loads(result.raw_output)["store"] == "Test Mart"
    request = server.requests[0]
    assert request["model"] == "vis:7b" and request["stream"] is False
    assert request["format"]["required"] == ["lines"]
    assert len(request["messages"][0]["images"]) == 1
    assert "consecutive parts" not in request["messages"][0]["content"]
    assert request["options"]["num_ctx"] == 8192


def test_landscape_photo_tries_the_other_rotation_when_output_is_unusable(serve, tmp_path):
    server = serve(["garbage", GOOD])
    result = extractor(server).extract([make_photo(tmp_path, (400, 200))])
    assert len(server.requests) == 2
    assert result.receipt.lines[0].raw_text == "MILK"


def test_portrait_photo_is_not_retried(serve, tmp_path):
    server = serve(["garbage"])
    with pytest.raises(ExtractionError, match="valid receipt"):
        extractor(server).extract([make_photo(tmp_path, (200, 400))])
    assert len(server.requests) == 1


def test_every_orientation_failing_is_a_failed_extraction(serve, tmp_path):
    server = serve(["garbage", json.dumps({"lines": []})])
    with pytest.raises(ExtractionError):
        extractor(server).extract([make_photo(tmp_path, (400, 200))])


def test_several_photos_go_in_one_request(serve, tmp_path):
    server = serve([GOOD])
    photos = [make_photo(tmp_path, name="a.jpg"), make_photo(tmp_path, name="b.jpg")]
    extractor(server).extract(photos)
    assert len(server.requests[0]["messages"][0]["images"]) == 2
    assert "consecutive parts" in server.requests[0]["messages"][0]["content"]


def test_very_tall_photo_is_tiled(serve, tmp_path):
    server = serve([GOOD])
    extractor(server).extract([make_photo(tmp_path, (200, 1500))])
    assert len(server.requests[0]["messages"][0]["images"]) > 1


def test_heic_photo_is_decoded(tmp_path):
    path = tmp_path / "p.heic"
    Image.new("RGB", (64, 96), "white").save(path, format="HEIF")
    assert images.load(path).size == (64, 96)


def test_unreadable_image_is_an_extraction_error(tmp_path):
    path = tmp_path / "bad.jpg"
    path.write_bytes(b"not an image")
    with pytest.raises(ExtractionError):
        images.load(path)


def test_unreachable_server_is_backend_unavailable(tmp_path):
    dead = OllamaVisionExtractor("http://127.0.0.1:1", "vis:7b", timeout=2)
    with pytest.raises(BackendUnavailable):
        dead.extract([make_photo(tmp_path)])


def test_model_not_installed_is_backend_unavailable(serve, tmp_path):
    server = serve([GOOD], models=[])
    with pytest.raises(BackendUnavailable, match="not installed"):
        extractor(server).extract([make_photo(tmp_path)])


def test_server_error_is_backend_unavailable(serve, tmp_path):
    server = serve([], chat_status=500)
    with pytest.raises(BackendUnavailable):
        extractor(server).extract([make_photo(tmp_path)])


def test_images_are_never_sent_to_a_public_address():
    with pytest.raises(ExtractionError, match="not on the local network"):
        require_local("http://8.8.8.8:11434")
    for ok in ("http://127.0.0.1:11434", "http://192.168.1.20:11434", "http://10.0.0.5"):
        require_local(ok)


def test_backend_is_registered_for_local_network_and_built_from_settings():
    assert "ollama-vision" in available()
    built = create("ollama-vision", Settings(vision_model="m:1", model_timeout_seconds=9))
    assert (built.model, built.timeout) == ("m:1", 9)


def sent_images(server):
    import base64
    import io

    return [
        Image.open(io.BytesIO(base64.b64decode(data)))
        for request in server.requests
        for message in request["messages"]
        for data in message["images"]
    ]


def marker_photo(tmp_path, turn, name="marked.png"):
    """A photo whose top-left is red, top-right blue, bottom-left green, turned by `turn`."""
    image = Image.new("RGB", (100, 200), "white")
    for x0, y0, colour in [(0, 0, "red"), (90, 0, "blue"), (0, 190, "green")]:
        image.paste(Image.new("RGB", (10, 10), colour), (x0, y0))
    path = tmp_path / name
    image.rotate(turn, expand=True).save(path)
    return path


def is_colour(pixel, wanted):
    r, g, b = pixel
    return {"red": r > 200 > g, "blue": b > 200 > r, "green": g > 100 > r}[wanted]


def test_the_model_is_sent_an_upright_image(serve, tmp_path):
    server = serve([GOOD])
    extractor(server).extract([marker_photo(tmp_path, turn=0)])
    (sent,) = sent_images(server)
    assert is_colour(sent.getpixel((3, 3)), "red")
    assert is_colour(sent.getpixel((sent.width - 4, 3)), "blue")
    assert is_colour(sent.getpixel((3, sent.height - 4)), "green")


def test_a_sideways_photo_is_turned_upright_before_it_is_sent(serve, tmp_path):
    server = serve([GOOD])
    extractor(server).extract([marker_photo(tmp_path, turn=-90)])
    (sent,) = sent_images(server)
    assert sent.height > sent.width
    assert is_colour(sent.getpixel((3, 3)), "red")
    assert is_colour(sent.getpixel((sent.width - 4, 3)), "blue")


def test_the_retry_sends_the_other_turn_not_the_same_image_again(serve, tmp_path):
    server = serve(["not json", GOOD])
    extractor(server).extract([marker_photo(tmp_path, turn=90)])
    first, second = sent_images(server)
    assert not is_colour(first.getpixel((3, 3)), "red")  # the first guess was the wrong turn
    assert is_colour(second.getpixel((3, 3)), "red")


def test_a_unit_without_a_quantity_or_unit_price_is_dropped():
    # The model sometimes puts the tax flag, or a size from the description, in `unit`.
    receipt = parse_receipt(
        json.dumps(
            {
                "lines": [
                    {"raw_text": "OAT MILK", "line_price": "4.49", "unit": "T"},
                    {"raw_text": "APPLES", "line_price": "1.50", "quantity": "2", "unit": "lb"},
                ]
            }
        )
    )
    assert [line.unit for line in receipt.lines] == [None, "lb"]
