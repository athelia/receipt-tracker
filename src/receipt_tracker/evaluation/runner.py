"""Run a backend over the golden photos and record how it did."""

import json
import time
import urllib.request
from collections.abc import Callable, Sequence
from urllib.error import URLError

from receipt_tracker.evaluation.runs import Run
from receipt_tracker.evaluation.scoring import score_receipt
from receipt_tracker.evaluation.truth import Case
from receipt_tracker.extractors.base import ExtractedReceipt, ExtractionError, Extractor

Probe = Callable[[], int | None]
Progress = Callable[[int, int, dict], None]


def vram_from_ps(payload: dict, model: str) -> int | None:
    """Bytes of GPU memory Ollama says `model` holds, from an `/api/ps` answer."""
    for loaded in payload.get("models", []):
        if model in (loaded.get("name"), loaded.get("model")):
            return loaded.get("size_vram")
    return None


def ollama_vram_probe(server_url: str, model: str) -> Probe:
    """A probe asking a local Ollama server how much GPU memory the model uses."""

    def probe() -> int | None:
        try:
            with urllib.request.urlopen(f"{server_url.rstrip('/')}/api/ps", timeout=5) as reply:
                return vram_from_ps(json.load(reply), model)
        except (URLError, OSError, ValueError):
            return None

    return probe


def run_case(extractor: Extractor, case: Case, probe: Probe | None = None) -> dict:
    """Extract one receipt and score it. A backend that cannot be reached is not a result:
    `BackendUnavailable` propagates so the run can be resumed once it is back."""
    truth = case.truth
    record: dict = {
        "key": truth.key,
        "store": truth.store,
        "photos": [photo.name for photo in case.photos],
    }
    started = time.monotonic()
    read: ExtractedReceipt | None = None
    try:
        result = extractor.extract(list(case.photos))
    except ExtractionError as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        record["raw_output"] = error.raw_output
    else:
        read = result.receipt
        record.update(status="ok", receipt=read.to_dict(), raw_output=result.raw_output)
        record.update(model=result.model, model_version=result.model_version)
    record["seconds"] = round(time.monotonic() - started, 1)
    record["vram_bytes"] = probe() if probe else None
    record["counts"] = score_receipt(read, truth)
    return record


def evaluate(
    extractor: Extractor,
    cases: Sequence[Case],
    run: Run,
    probe: Probe | None = None,
    progress: Progress | None = None,
) -> int:
    """Run every case not already in `run`, appending each result as it finishes.

    Returns how many cases ran this time.
    """
    done = run.done_keys()
    ran = 0
    for index, case in enumerate(cases, 1):
        if case.truth.key in done:
            continue
        record = run_case(extractor, case, probe)
        run.append(record)
        ran += 1
        if progress:
            progress(index, len(cases), record)
    return ran


def rescore(run: Run, truth_by_key: dict) -> int:
    """Score the stored answers again, e.g. after the scoring rules changed.

    Returns how many receipts were rescored. A receipt whose truth is gone keeps its old counts.
    """
    records, changed = run.records(), 0
    for record in records:
        truth = truth_by_key.get(record["key"])
        if truth is None:
            continue
        read = ExtractedReceipt.from_dict(record["receipt"]) if record.get("receipt") else None
        record["counts"] = score_receipt(read, truth)
        changed += 1
    run.rewrite(records)
    return changed
