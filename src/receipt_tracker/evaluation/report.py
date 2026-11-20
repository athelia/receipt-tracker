"""Turn stored results into the comparison tables.

Figures are shown as percentages, and as "n/a" when there was nothing to measure
(for example the quantity match of a store whose lines print no quantity).
"""

import statistics
from collections import defaultdict
from collections.abc import Sequence

from receipt_tracker.evaluation.runs import Run
from receipt_tracker.evaluation.scoring import add_counts, metrics

ALL = "ALL"

# (heading, metric) per table; the first columns of every table are the group and receipts.
TABLES: dict[str, list[tuple[str, str]]] = {
    "Lines": [
        ("recall", "line_recall"),
        ("precision", "line_precision"),
        ("price R", "price_recall"),
        ("price P", "price_precision"),
        ("code R", "code_recall"),
        ("code P", "code_precision"),
        ("code CER", "code_cer"),
    ],
    "Fields of matched lines": [
        ("quantity", "quantity_match"),
        ("unit price", "unit_price_match"),
        ("discount", "discount_match"),
        ("name CER", "text_cer"),
        ("store", "store_match"),
        ("date", "date_match"),
        ("receipt #", "number_match"),
    ],
    "Consistency (pass rate)": [
        ("lines = subtotal", "sum_check_pass"),
        ("savings = discounts", "savings_check_pass"),
    ],
}

NOTES = (
    "R = recall, P = precision, CER = character error rate (lower is better).",
    "Lines are matched in printed order, by exact code or exact price (or a near code with a "
    "similar name).",
    "Name CER compares with the workbook's item name, which is typed in, not copied from the "
    "receipt: treat it as a rough signal.",
    "The workbook has no subtotal, total, payment method or savings figure, so those are only "
    "used in the consistency checks.",
)


def summarize(records: Sequence[dict]) -> dict[str, dict]:
    """Figures per store and for all receipts together."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        groups[record["store"]].append(record)
        groups[ALL].append(record)
    summary = {}
    for name, members in groups.items():
        counts = add_counts(*(record["counts"] for record in members))
        seconds = [record["seconds"] for record in members if record.get("seconds") is not None]
        vram = [record["vram_bytes"] for record in members if record.get("vram_bytes")]
        summary[name] = {
            "receipts": len(members),
            "failed": sum(1 for record in members if record["status"] != "ok"),
            "counts": counts,
            "metrics": metrics(counts),
            "seconds_mean": statistics.fmean(seconds) if seconds else None,
            "seconds_median": statistics.median(seconds) if seconds else None,
            "seconds_max": max(seconds) if seconds else None,
            "vram_gb": max(vram) / 1e9 if vram else None,
        }
    return summary


def ordered(summary: dict[str, dict]) -> list[str]:
    """Stores by name, then the total."""
    return sorted(name for name in summary if name != ALL) + [ALL]


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _render(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(headers, widths, strict=True))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append(
            "  ".join(
                str(cell).ljust(width) if index == 0 else str(cell).rjust(width)
                for index, (cell, width) in enumerate(zip(row, widths, strict=True))
            )
        )
    return lines


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def render_run(run: Run) -> str:
    """The full report for one run."""
    meta, records = run.meta, run.records()
    if not records:
        return f"{run.run_id}: no results yet"
    summary = summarize(records)
    names = ordered(summary)
    out = [f"Run {run.run_id}", _settings_line(meta), ""]
    for title, columns in TABLES.items():
        headers = ["", "receipts", "failed"] + [heading for heading, _ in columns]
        rows = [
            [name, str(summary[name]["receipts"]), str(summary[name]["failed"])]
            + [percent(summary[name]["metrics"][metric]) for _, metric in columns]
            for name in names
        ]
        out += [title, *_render(headers, rows), ""]
    headers = ["", "mean s", "median s", "max s", "VRAM GB"]
    rows = [
        [
            name,
            _seconds(summary[name]["seconds_mean"]),
            _seconds(summary[name]["seconds_median"]),
            _seconds(summary[name]["seconds_max"]),
            "n/a" if summary[name]["vram_gb"] is None else f"{summary[name]['vram_gb']:.1f}",
        ]
        for name in names
    ]
    out += ["Cost per receipt", *_render(headers, rows), "", *NOTES]
    return "\n".join(out)


def _settings_line(meta: dict) -> str:
    settings = ", ".join(
        f"{key}={value}" for key, value in sorted(meta.get("settings", {}).items())
    )
    return f"backend={meta.get('backend')}  {settings}"


def render_comparison(runs: Sequence[Run], store: str = ALL) -> str:
    """Runs side by side for one store (or all of them): which settings differ, then each figure."""
    metas = [run.meta for run in runs]
    keys = sorted({key for meta in metas for key in meta.get("settings", {})})
    differing = [
        key for key in keys if len({str(meta.get("settings", {}).get(key)) for meta in metas}) > 1
    ]
    # Only receipts every run covers are compared, so a run over fewer receipts is not flattered.
    everywhere = set.intersection(*(run.done_keys() for run in runs))
    summaries = []
    for run in runs:
        summary = summarize([r for r in run.records() if r["key"] in everywhere])
        if store not in summary:
            raise KeyError(f"No receipts for {store!r} that all of these runs cover")
        summaries.append(summary[store])

    headers = [f"{store}, {len(runs)} runs"] + [run.run_id for run in runs]
    rows = [["backend"] + [str(meta.get("backend")) for meta in metas]]
    rows += [
        [key] + [str(meta.get("settings", {}).get(key)) for meta in metas] for key in differing
    ]
    rows.append(["receipts"] + [str(s["receipts"]) for s in summaries])
    rows.append(["failed"] + [str(s["failed"]) for s in summaries])
    for _, columns in TABLES.items():
        for heading, metric in columns:
            rows.append([heading] + [percent(s["metrics"][metric]) for s in summaries])
    rows.append(["mean s"] + [_seconds(s["seconds_mean"]) for s in summaries])
    rows.append(
        ["VRAM GB"] + ["n/a" if s["vram_gb"] is None else f"{s['vram_gb']:.1f}" for s in summaries]
    )
    return "\n".join(_render(headers, rows))
