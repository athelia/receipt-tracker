"""The evaluation harness: scoring, pairing photos with truth, stored runs, reports, CLI.

Scoring is tested with small hand-made receipts whose right answers can be counted by eye.
"""

import json

import pytest
from typer.testing import CliRunner

from receipt_tracker import db
from receipt_tracker.cli import app
from receipt_tracker.config import Settings, get_settings
from receipt_tracker.evaluation import report, runner, runs, scoring, truth
from receipt_tracker.evaluation.cli import recorded, with_overrides
from receipt_tracker.extractors.base import (
    BackendUnavailable,
    ExtractedHeader,
    ExtractedLine,
    ExtractedReceipt,
    ExtractionError,
    ExtractionResult,
)


def want(code, name, price, **fields):
    return truth.TruthLine(
        item_code=code,
        name=name,
        quantity_milli=fields.get("quantity_milli"),
        unit_price_cents=fields.get("unit_price_cents"),
        discount_bp=fields.get("discount_bp"),
        line_price_cents=price,
    )


def read(name, price, code=None, **fields):
    return ExtractedLine(name, price, item_code=code, **fields)


def receipt(*lines, number="A-17", store="YDFM", date="2026-03-04"):
    return truth.TruthReceipt(1, store, date, number, tuple(lines))


# --- strings -------------------------------------------------------------------


def test_edit_distance_counts_single_character_changes():
    assert scoring.edit_distance("kitten", "sitting") == 3
    assert scoring.edit_distance("", "abc") == 3 and scoring.edit_distance("abc", "") == 3
    assert scoring.edit_distance("same", "same") == 0


def test_character_error_rate_is_relative_to_the_true_length():
    assert scoring.char_error_rate("12345", "12945") == pytest.approx(0.2)
    assert scoring.char_error_rate("12", "") == 1.0
    assert scoring.char_error_rate("", "") == 0.0


def test_codes_ignore_spaces_and_case_and_descriptions_ignore_punctuation():
    assert scoring.normalize_code(" ab 12 ") == "AB12"
    assert scoring.normalize_text("Oat  Milk, 1/2 gal.") == "oat milk 1 2 gal"
    assert scoring.normalize_store("Buford Hwy F.M.") == "bufordhwyfm"


# --- lines -----------------------------------------------------------------------


def test_a_perfect_read_pairs_every_line():
    truth_lines = [want("111", "MILK", 350), want("222", "BREAD", 200)]
    read_lines = [read("MILK", 350, "111"), read("BREAD", 200, "222")]
    assert len(scoring.align_lines(read_lines, truth_lines)) == 2


def test_a_dropped_line_costs_recall_and_an_invented_one_costs_precision():
    truth_lines = [want("111", "MILK", 350), want("222", "BREAD", 200), want("333", "EGGS", 400)]
    read_lines = [read("MILK", 350, "111"), read("BAGS", 25), read("EGGS", 400, "333")]
    pairs = scoring.align_lines(read_lines, truth_lines)
    assert [got.raw_text for got, _ in pairs] == ["MILK", "EGGS"]  # BREAD missing, BAGS invented


def test_identical_lines_are_not_credited_twice_when_the_model_collapses_them():
    truth_lines = [want("111", "MILK", 350)] * 3
    read_lines = [read("MILK", 350, "111")] * 2  # the third was merged away
    assert len(scoring.align_lines(read_lines, truth_lines)) == 2


def test_a_misread_code_still_lines_up_with_the_right_line():
    pairs = scoring.align_lines([read("MILK", 999, "11234")], [want("11235", "MILK", 350)])
    assert len(pairs) == 1  # one digit off and the same name, though the price differs


def test_a_similar_name_alone_or_a_near_code_alone_is_not_the_same_line():
    milk = want("11235", "MILK", 350)
    assert scoring.align_lines([read("MILK", 999)], [milk]) == []  # same name, nothing else
    assert scoring.align_lines([read("SOAP", 999, "11234")], [milk]) == []  # near code only


def test_unrelated_lines_do_not_pair():
    assert scoring.align_lines([read("SOAP", 100, "9")], [want("111", "MILK", 350)]) == []


def test_lines_pair_in_printed_order():
    truth_lines = [want("1", "A", 100), want("2", "B", 200)]
    read_lines = [read("B", 200, "2"), read("A", 100, "1")]  # reversed
    assert len(scoring.align_lines(read_lines, truth_lines)) == 1  # only one fits in order


# --- one receipt ---------------------------------------------------------------------


GOLDEN = receipt(
    want("111", "MILK", 350),
    want("222", "APPLES", 150, quantity_milli=340, unit_price_cents=441, discount_bp=2000),
    want(None, "BREAD", 200),
)


def test_counts_for_a_hand_checked_receipt():
    result = ExtractedReceipt(
        header=ExtractedHeader(
            store_name="ydfm", purchased_at="2026-03-04T12:47", receipt_number="a-17"
        ),
        lines=(
            read("MILK", 350, "111"),
            read("APPLE", 150, "227", quantity_milli=340, unit_price_cents=441),  # code off by 1
            read("BAGS", 25),  # invented; BREAD was dropped
        ),
    )
    counts = scoring.score_receipt(result, GOLDEN)
    assert (counts["truth_lines"], counts["extracted_lines"], counts["matched_lines"]) == (3, 3, 2)
    assert counts["price_hits"] == 2
    assert (counts["code_truth"], counts["code_extracted"], counts["code_hits"]) == (2, 2, 1)
    assert (counts["code_edits"], counts["code_chars"]) == (1, 6)
    assert (counts["text_edits"], counts["text_chars"]) == (1, 10)  # "apple" for "apples"
    assert (counts["quantity_truth"], counts["quantity_hits"]) == (1, 1)
    assert (counts["unit_price_truth"], counts["unit_price_hits"]) == (1, 1)
    assert (counts["discount_truth"], counts["discount_hits"]) == (1, 0)  # discount not read
    assert [counts[f"{name}_hits"] for name in ("store", "date", "number")] == [1, 1, 1]


def test_the_figures_follow_from_the_counts():
    result = ExtractedReceipt(lines=(read("MILK", 350, "111"), read("BAGS", 25)))
    figures = scoring.metrics(scoring.score_receipt(result, GOLDEN))
    assert figures["line_recall"] == pytest.approx(1 / 3)
    assert figures["line_precision"] == pytest.approx(1 / 2)
    assert figures["code_recall"] == pytest.approx(1 / 2)  # 1 of the 2 coded truth lines
    assert figures["code_precision"] == 1.0
    assert figures["quantity_match"] == 0.0  # APPLES' quantity was there to find and was not


def test_a_failed_extraction_scores_zero_hits_but_keeps_what_was_there_to_find():
    counts = scoring.score_receipt(None, GOLDEN)
    assert counts["truth_lines"] == 3 and counts["code_truth"] == 2
    assert counts["matched_lines"] == counts["extracted_lines"] == 0
    assert (counts["store_truth"], counts["store_hits"], counts["number_truth"]) == (1, 0, 1)


def test_header_fields_compare_loosely_where_the_workbook_is_loose():
    header = ExtractedHeader(store_name="Y.D.F.M", purchased_at="2026-03-05", receipt_number=None)
    counts = scoring.score_receipt(ExtractedReceipt(header, ()), GOLDEN)
    assert (counts["store_hits"], counts["date_hits"], counts["number_hits"]) == (1, 0, 0)


def test_a_receipt_without_a_number_has_nothing_to_score_for_it():
    counts = scoring.score_receipt(ExtractedReceipt(), receipt(number=""))
    assert counts["number_truth"] == 0 and scoring.metrics(counts)["number_match"] is None


def test_the_line_sum_check_compares_with_the_printed_subtotal():
    lines = (read("A", 350), read("B", 150))
    passing = scoring.score_receipt(
        ExtractedReceipt(ExtractedHeader(subtotal_cents=500), lines), GOLDEN
    )
    failing = scoring.score_receipt(
        ExtractedReceipt(ExtractedHeader(subtotal_cents=525), lines), GOLDEN
    )
    unchecked = scoring.score_receipt(ExtractedReceipt(ExtractedHeader(), lines), GOLDEN)
    assert (passing["sum_checked"], passing["sum_passed"]) == (1, 1)
    assert (failing["sum_checked"], failing["sum_passed"]) == (1, 0)
    assert unchecked["sum_checked"] == 0


def test_implied_savings_come_from_the_discounts_on_net_prices():
    lines = [read("A", 800, discount_bp=2000), read("B", 100)]  # 20% off: paid 8.00, saved 2.00
    assert scoring.implied_savings_cents(lines) == 200


def test_the_savings_check_allows_a_cent_of_rounding_per_discounted_line():
    lines = (read("A", 801, discount_bp=2000),)  # implied 200.25 -> 200
    close = ExtractedReceipt(ExtractedHeader(savings_cents=201), lines)
    far = ExtractedReceipt(ExtractedHeader(savings_cents=260), lines)
    assert scoring.score_receipt(close, GOLDEN)["savings_passed"] == 1
    assert scoring.score_receipt(far, GOLDEN)["savings_passed"] == 0
    assert scoring.score_receipt(ExtractedReceipt(), GOLDEN)["savings_checked"] == 0


def test_summed_counts_weigh_a_long_receipt_more_than_a_short_one():
    long = {**dict.fromkeys(scoring.COUNT_FIELDS, 0), "truth_lines": 60, "matched_lines": 60}
    short = {**dict.fromkeys(scoring.COUNT_FIELDS, 0), "truth_lines": 3, "matched_lines": 0}
    total = scoring.add_counts(long, short)
    assert scoring.metrics(total)["line_recall"] == pytest.approx(60 / 63)  # not (1 + 0) / 2


# --- ground truth and photos ---------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "golden.db")
    db.migrate(connection)
    yield connection
    connection.close()


def add_receipt(conn, store, date, number, lines=(), origin="golden"):
    conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (store,))
    store_id = conn.execute("SELECT id FROM stores WHERE name = ?", (store,)).fetchone()[0]
    receipt_id = conn.execute(
        "INSERT INTO receipts (store_id, purchased_at, receipt_number, origin) VALUES (?,?,?,?)",
        (store_id, date, number, origin),
    ).lastrowid
    for position, (code, name, price) in enumerate(lines):
        conn.execute(
            "INSERT INTO line_items (receipt_id, position, item_code, raw_text, line_price_cents)"
            " VALUES (?,?,?,?,?)",
            (receipt_id, position, code, name, price),
        )
    return receipt_id


def test_truth_is_the_golden_receipts_with_their_lines_in_printed_order(conn):
    add_receipt(conn, "YDFM", "2026-03-04", "A-17", [("1", "MILK", 350), (None, "BREAD", 200)])
    add_receipt(conn, "YDFM", "2026-03-05", "B-1", [("9", "OTHER", 1)], origin="extracted")
    (one,) = truth.load_truth(conn)
    assert (one.store, one.date, one.number) == ("YDFM", "2026-03-04", "A-17")
    assert [(line.item_code, line.name, line.line_price_cents) for line in one.lines] == [
        ("1", "MILK", 350),
        (None, "BREAD", 200),
    ]
    assert one.key == "2026-03-04_YDFM_A-17"


def write_manifest(folder, rows):
    path = folder / "manifest.csv"
    header = "photo,sequence,store,purchased_at,receipt_number,matched_by\n"
    path.write_text(header + "".join(",".join(map(str, row)) + "\n" for row in rows))
    return path


def touch(folder, *names):
    (folder / "photos").mkdir(exist_ok=True)
    for name in names:
        (folder / "photos" / name).write_bytes(b"x")


def test_photos_are_paired_with_receipts_and_a_long_receipt_keeps_its_photo_order(conn, tmp_path):
    add_receipt(conn, "YDFM", "2026-03-04", "A-17", [("1", "MILK", 350)])
    touch(tmp_path, "top.jpg", "bottom.jpg")
    manifest = write_manifest(
        tmp_path,
        [
            ("photos/bottom.jpg", 2, "YDFM", "2026-03-04", "A-17", "receipt number"),
            ("photos/top.jpg", 1, "YDFM", "2026-03-04", "A-17", "receipt number"),
        ],
    )
    paired = truth.pair_photos(manifest, truth.load_truth(conn))
    (case,) = paired.cases
    assert [photo.name for photo in case.photos] == ["top.jpg", "bottom.jpg"]
    assert paired.skipped == ()


def test_photos_that_cannot_be_paired_are_left_out_with_the_reason(conn, tmp_path):
    add_receipt(conn, "YDFM", "2026-03-04", "A-17")
    add_receipt(conn, "YDFM", "2026-03-04", "A-18")  # two receipts that day
    touch(tmp_path, "stranger.jpg", "ambiguous.jpg")
    manifest = write_manifest(
        tmp_path,
        [
            ("photos/stranger.jpg", 1, "", "", "", "not in workbook"),
            ("photos/gone.jpg", 1, "YDFM", "2026-03-04", "A-17", "receipt number"),
            ("photos/ambiguous.jpg", 1, "YDFM", "2026-03-04", "Z", "date"),
        ],
    )
    paired = truth.pair_photos(manifest, truth.load_truth(conn))
    assert paired.cases == ()
    reasons = " | ".join(paired.skipped)
    assert "stranger.jpg: not in the workbook" in reasons
    assert "gone.jpg: file is missing" in reasons
    assert "ambiguous.jpg: no single golden receipt matches" in reasons


def test_a_photo_matched_by_date_alone_pairs_when_only_one_receipt_that_day(conn, tmp_path):
    add_receipt(conn, "Max", "2026-05-13", None)
    touch(tmp_path, "max.jpg")
    manifest = write_manifest(tmp_path, [("photos/max.jpg", 1, "Max", "2026-05-13", "", "date")])
    assert len(truth.pair_photos(manifest, truth.load_truth(conn)).cases) == 1


# --- runs on disk ---------------------------------------------------------------------


def test_a_run_keeps_its_meta_and_appends_results_in_order(tmp_path):
    run = runs.create_run(tmp_path, "r1", {"backend": "fake", "settings": {"vision_crop": True}})
    run.append({"key": "a"})
    run.append({"key": "b"})
    again = runs.open_run(tmp_path, "r1")
    assert [record["key"] for record in again.records()] == ["a", "b"]
    assert again.done_keys() == {"a", "b"} and again.meta["backend"] == "fake"


def test_runs_are_listed_oldest_first_and_an_unknown_one_is_an_error(tmp_path):
    runs.create_run(tmp_path, "20260102_x", {})
    runs.create_run(tmp_path, "20260101_y", {})
    assert [run.run_id for run in runs.list_runs(tmp_path)] == ["20260101_y", "20260102_x"]
    with pytest.raises(FileNotFoundError):
        runs.open_run(tmp_path, "nope")


def test_a_run_id_names_the_backend_and_an_optional_label():
    assert runs.new_run_id("fake", "crop-off").endswith("_fake_crop-off")
    assert runs.new_run_id("fake").endswith("_fake")


# --- running ---------------------------------------------------------------------------


def case_for(tmp_path, number="A-17", lines=(("111", "MILK", 350),)):
    photo = tmp_path / f"{number}.jpg"
    photo.write_bytes(b"x")
    golden = truth.TruthReceipt(
        1, "YDFM", "2026-03-04", number, tuple(want(*line) for line in lines)
    )
    return truth.Case(golden, (photo,))


class Scripted:
    """An extractor that answers each call from a list of results or errors."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def extract(self, photos):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ExtractionResult(answer, "fake", "m", "1", raw_output="raw")


MILK = ExtractedReceipt(ExtractedHeader(store_name="YDFM"), (read("MILK", 350, "111"),))


def test_a_receipt_is_read_scored_timed_and_stored(tmp_path):
    run = runs.create_run(tmp_path / "runs", "r", {})
    ran = runner.evaluate(Scripted(MILK), [case_for(tmp_path)], run, probe=lambda: 5_000_000_000)
    (record,) = run.records()
    assert ran == 1 and record["status"] == "ok" and record["key"] == "2026-03-04_YDFM_A-17"
    assert record["counts"]["matched_lines"] == 1 and record["counts"]["price_hits"] == 1
    assert record["seconds"] >= 0 and record["vram_bytes"] == 5_000_000_000
    assert (record["model"], record["model_version"]) == ("m", "1")
    assert record["receipt"]["lines"][0]["raw_text"] == "MILK"


def test_a_failed_extraction_is_recorded_with_what_the_model_said(tmp_path):
    run = runs.create_run(tmp_path / "runs", "r", {})
    error = ExtractionError("not json", raw_output="garbage")
    runner.evaluate(Scripted(error), [case_for(tmp_path)], run)
    (record,) = run.records()
    assert record["status"] == "failed" and "not json" in record["error"]
    assert record["raw_output"] == "garbage" and record["counts"]["matched_lines"] == 0
    assert record["counts"]["truth_lines"] == 1  # the miss still counts against recall


def test_a_resumed_run_skips_what_is_done_and_an_unreachable_model_stops_it(tmp_path):
    run = runs.create_run(tmp_path / "runs", "r", {})
    cases = [case_for(tmp_path, "A-1"), case_for(tmp_path, "A-2"), case_for(tmp_path, "A-3")]
    with pytest.raises(BackendUnavailable):
        runner.evaluate(Scripted(MILK, BackendUnavailable("down")), cases, run)
    assert len(run.records()) == 1  # the first result survived

    second = Scripted(MILK, MILK)
    assert runner.evaluate(second, cases, run) == 2
    assert second.calls == 2 and len(run.records()) == 3


def test_the_progress_callback_sees_each_receipt(tmp_path):
    seen = []
    run = runs.create_run(tmp_path / "runs", "r", {})
    cases = [case_for(tmp_path, "A-1"), case_for(tmp_path, "A-2")]
    runner.evaluate(Scripted(MILK, MILK), cases, run, progress=lambda i, n, r: seen.append((i, n)))
    assert seen == [(1, 2), (2, 2)]


def test_gpu_memory_is_read_from_the_ollama_process_list():
    payload = {"models": [{"name": "other:1b", "size_vram": 1}, {"name": "vis:7b", "size_vram": 6}]}
    assert runner.vram_from_ps(payload, "vis:7b") == 6
    assert runner.vram_from_ps(payload, "absent") is None
    assert runner.vram_from_ps({}, "vis:7b") is None


def test_an_unreachable_server_gives_no_memory_figure_instead_of_an_error():
    assert runner.ollama_vram_probe("http://127.0.0.1:1", "vis:7b")() is None


def test_rescoring_applies_the_current_rules_to_the_stored_answers(tmp_path):
    run = runs.create_run(tmp_path / "runs", "r", {})
    case = case_for(tmp_path)
    runner.evaluate(Scripted(MILK), [case], run)
    corrected = truth.TruthReceipt(
        case.truth.receipt_id, "YDFM", "2026-03-04", "A-17", (want("111", "MILK", 999),)
    )
    assert runner.rescore(run, {case.truth.key: corrected}) == 1
    assert run.records()[0]["counts"]["price_hits"] == 0  # 350 no longer matches 999


# --- reports -----------------------------------------------------------------------------


def stored_run(tmp_path, name, settings, results):
    """A run holding (store, key, extracted-or-None, seconds) results scored against one line."""
    run = runs.create_run(tmp_path, name, {"backend": "fake", "settings": settings})
    for store, key, got, seconds in results:
        golden = truth.TruthReceipt(1, store, "2026-03-04", key, (want("111", "MILK", 350),))
        run.append(
            {
                "key": key,
                "store": store,
                "status": "ok" if got else "failed",
                "seconds": seconds,
                "vram_bytes": 6_000_000_000,
                "counts": scoring.score_receipt(got, golden),
            }
        )
    return run


WRONG = ExtractedReceipt(ExtractedHeader(), (read("MILK", 351, "111"),))


def test_the_summary_has_a_row_per_store_and_a_total(tmp_path):
    run = stored_run(
        tmp_path,
        "r",
        {},
        [("YDFM", "a", MILK, 100), ("YDFM", "b", None, 300), ("Publix", "c", MILK, 50)],
    )
    summary = report.summarize(run.records())
    assert report.ordered(summary) == ["Publix", "YDFM", "ALL"]
    assert (summary["YDFM"]["receipts"], summary["YDFM"]["failed"]) == (2, 1)
    assert summary["ALL"]["metrics"]["line_recall"] == pytest.approx(2 / 3)
    assert summary["YDFM"]["seconds_mean"] == 200 and summary["ALL"]["seconds_max"] == 300
    assert summary["ALL"]["vram_gb"] == 6.0


def test_the_report_states_recall_precision_and_code_error_rate_with_n_a_for_nothing(tmp_path):
    run = stored_run(tmp_path, "r", {"vision_crop": True}, [("YDFM", "a", MILK, 10)])
    text = report.render_run(run)
    for heading in ("recall", "precision", "code CER", "Consistency", "Cost per receipt"):
        assert heading in text
    assert "100.0%" in text and "n/a" in text  # no subtotal was read, so the check is n/a
    assert "vision_crop=True" in text


def test_a_run_with_no_results_says_so(tmp_path):
    run = runs.create_run(tmp_path, "empty", {"backend": "fake", "settings": {}})
    assert "no results yet" in report.render_run(run)


def test_comparison_shows_only_the_settings_that_differ_and_only_shared_receipts(tmp_path):
    one = stored_run(
        tmp_path, "one", {"vision_crop": True, "vision_num_ctx": 8192}, [("YDFM", "a", MILK, 10)]
    )
    two = stored_run(
        tmp_path,
        "two",
        {"vision_crop": False, "vision_num_ctx": 8192},
        [("YDFM", "a", WRONG, 20), ("YDFM", "b", MILK, 20)],  # b was not run in `one`
    )
    text = report.render_comparison([one, two])
    assert "vision_crop" in text and "vision_num_ctx" not in text
    assert "100.0%" in text and "0.0%" in text
    receipts_row = next(line for line in text.splitlines() if line.startswith("receipts"))
    assert receipts_row.split()[1:] == ["1", "1"]


def test_comparing_for_a_store_nobody_ran_is_an_error(tmp_path):
    one = stored_run(tmp_path, "one", {}, [("YDFM", "a", MILK, 10)])
    with pytest.raises(KeyError):
        report.render_comparison([one], store="Publix")


# --- the command ---------------------------------------------------------------------------


def test_settings_overrides_are_validated_and_only_run_settings_are_recorded():
    base = Settings(_env_file=None)
    changed = with_overrides(base, ["vision_crop=false", "vision_max_aspect=2.5"])
    assert changed.vision_crop is False and changed.vision_max_aspect == 2.5
    assert base.vision_crop is True  # the original is untouched
    kept = recorded(changed)
    assert kept["vision_crop"] is False and "data_dir" not in kept and "port" not in kept
    for bad in ("nonsense=1", "vision_crop"):
        with pytest.raises(Exception, match="expected NAME=VALUE"):
            with_overrides(base, [bad])


@pytest.fixture
def golden_env(tmp_path, monkeypatch):
    """A data folder with one golden receipt and its photo, and settings pointing at it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECEIPT_TRACKER_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("RECEIPT_TRACKER_GOLDEN_MANIFEST", str(tmp_path / "manifest.csv"))
    (tmp_path / "var").mkdir()
    settings_db = db.connect(tmp_path / "var" / "receipt_tracker.db")
    db.migrate(settings_db)
    add_receipt(settings_db, "Fake Store", "2026-03-04", "A-17", [("111", "MILK", 200)])
    settings_db.commit()
    settings_db.close()
    touch(tmp_path, "a.jpg")
    write_manifest(tmp_path, [("photos/a.jpg", 1, "Fake Store", "2026-03-04", "A-17", "number")])
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_one_command_runs_a_backend_and_prints_the_comparison_table(golden_env):
    result = CliRunner().invoke(app, ["evaluate", "run", "fake", "--label", "first"])
    assert result.exit_code == 0, result.output
    assert "[1/1]" in result.output and "Lines" in result.output and "code CER" in result.output
    (stored,) = runs.list_runs(golden_env / "var" / "eval")
    assert stored.run_id.endswith("_fake_first")
    assert stored.records()[0]["counts"]["matched_lines"] >= 1  # MILK 200 is on the receipt


def test_a_stored_run_can_be_listed_shown_compared_and_rescored(golden_env):
    cli = CliRunner()
    cli.invoke(app, ["evaluate", "run", "fake", "--label", "a"])
    cli.invoke(app, ["evaluate", "run", "fake", "--label", "b", "--set", "vision_crop=false"])
    first, second = (stored.run_id for stored in runs.list_runs(golden_env / "var" / "eval"))

    assert first in cli.invoke(app, ["evaluate", "list"]).output
    assert "Cost per receipt" in cli.invoke(app, ["evaluate", "show", first]).output
    compared = cli.invoke(app, ["evaluate", "compare", first, second]).output
    assert "vision_crop" in compared
    assert "1 receipts rescored" in cli.invoke(app, ["evaluate", "rescore", first]).output


def test_a_run_can_only_be_resumed_with_the_same_settings(golden_env):
    cli = CliRunner()
    cli.invoke(app, ["evaluate", "run", "fake"])
    (stored,) = runs.list_runs(golden_env / "var" / "eval")
    same = cli.invoke(app, ["evaluate", "run", "fake", "--resume", stored.run_id])
    other = cli.invoke(
        app, ["evaluate", "run", "fake", "--resume", stored.run_id, "--set", "vision_crop=false"]
    )
    assert same.exit_code == 0 and "[1/1]" not in same.output  # already done, nothing re-run
    assert other.exit_code == 1


def test_without_golden_receipts_the_command_says_what_to_do_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECEIPT_TRACKER_DATA_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["evaluate", "run", "fake"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1 and "import-golden" in result.output


def test_stored_records_are_plain_json(tmp_path):
    run = runs.create_run(tmp_path / "runs", "r", {})
    runner.evaluate(Scripted(MILK), [case_for(tmp_path)], run)
    line = (run.folder / "results.jsonl").read_text().splitlines()[0]
    assert json.loads(line)["counts"]["truth_lines"] == 1
