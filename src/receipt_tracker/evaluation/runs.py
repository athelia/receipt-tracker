"""Evaluation runs on disk, so settings can be compared from one run to the next.

A run is a folder holding `meta.json` (what was run, with which settings) and
`results.jsonl` (one line per receipt, appended as each finishes). A run that
stops partway can be resumed: receipts already in the file are not run again.
The files can hold receipt text, so they live under the private data folder.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Run:
    folder: Path

    @property
    def run_id(self) -> str:
        return self.folder.name

    @property
    def meta(self) -> dict:
        return json.loads((self.folder / "meta.json").read_text())

    def records(self) -> list[dict]:
        path = self.folder / "results.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def done_keys(self) -> set[str]:
        return {record["key"] for record in self.records()}

    def append(self, record: dict) -> None:
        with (self.folder / "results.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def rewrite(self, records: list[dict]) -> None:
        text = "".join(json.dumps(record) + "\n" for record in records)
        (self.folder / "results.jsonl").write_text(text)


def new_run_id(backend: str, label: str | None = None) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return "_".join(part for part in (stamp, backend, label) if part)


def create_run(root: Path, run_id: str, meta: dict) -> Run:
    folder = root / run_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    return Run(folder)


def open_run(root: Path, run_id: str) -> Run:
    folder = root / run_id
    if not (folder / "meta.json").exists():
        raise FileNotFoundError(f"No evaluation run named {run_id!r} in {root}")
    return Run(folder)


def list_runs(root: Path) -> list[Run]:
    """Runs, oldest first (ids start with a timestamp)."""
    if not root.exists():
        return []
    return [Run(folder) for folder in sorted(root.iterdir()) if (folder / "meta.json").exists()]
