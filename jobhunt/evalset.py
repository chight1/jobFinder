"""The extraction test set: postings with known-correct facts, to measure the model against.

Each file in tests/extraction_set holds one posting exactly as the model reads it, and its labels:
    {"job_id": 12, "source": "tokyodev", "posting": {title, company, location, source_fields, description},
     "facts": {...the Facts fields...} or null until labeled}
The posting is a snapshot, so the set keeps working after the database changes or is rebuilt.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt import score
from jobhunt.config import Config
from jobhunt.llm import LLM, Facts, InvalidOutput, RequestRejected, extract_posting, load_posting

MIN_DESCRIPTION_CHARS = 200
_NUMBERS = frozenset({"office_days_per_week", "years_required", "pay_min", "pay_max"})
_TEXTS = frozenset({"office_city", "pay_currency"})
_NOT_SCORED = frozenset({"evidence", "stack"})  # evidence is free text; stack is scored as recall


def set_dir(cfg: Config) -> Path:
    return cfg.root / "tests" / "extraction_set"


def export(conn: sqlite3.Connection, directory: Path, count: int) -> int:
    """Write up to `count` postings for labeling, taking sources in turn so no one site dominates.

    Only jobs the model would read count: code-removed ones never reach it. Existing files are kept.
    """
    rows = conn.execute(
        "SELECT j.id, s.source_id FROM jobs j JOIN sightings s ON s.id = j.primary_sighting_id "
        "WHERE j.stage IN ('queued', 'kept', 'removed', 'extract_failed') AND length(s.description) >= ? "
        "ORDER BY j.id",
        (MIN_DESCRIPTION_CHARS,),
    ).fetchall()
    by_source: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        by_source[row["source_id"]].append(row["id"])

    directory.mkdir(parents=True, exist_ok=True)
    existing = len(list(directory.glob("*.json")))
    written = 0
    queues = list(by_source.items())
    while existing + written < count and any(ids for _, ids in queues):
        for source_id, ids in queues:
            if not ids or existing + written >= count:
                continue
            job_id = ids.pop(0)
            path = directory / f"{job_id}.json"
            if path.exists():
                continue
            label = {"job_id": job_id, "source": source_id, "posting": load_posting(conn, job_id), "facts": None}
            path.write_text(json.dumps(label, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written += 1
    return written


@dataclass
class Report:
    labeled: int = 0
    failed: list[str] = field(default_factory=list)  # files the model gave no valid answer for
    right: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stack_recall: list[float] = field(default_factory=list)
    misses: list[tuple[str, str, Any, Any]] = field(default_factory=list)  # file, field, label, model

    def lines(self) -> list[str]:
        out = [f"{self.labeled} labeled postings, {len(self.failed)} with no valid answer from the model", ""]
        for name in sorted(self.total, key=lambda n: self.right[n] / self.total[n]):
            right, total = self.right[name], self.total[name]
            out.append(f"{name:24} {right:3}/{total:<3} {100 * right / total:5.1f}%")
        if self.stack_recall:
            recall = sum(self.stack_recall) / len(self.stack_recall)
            out.append(f"{'stack (labeled found)':24} {'':7} {100 * recall:5.1f}%")
        if self.misses:
            out += ["", "Misses:"]
            out += [f"  {file}  {name}: label {want!r}, model {got!r}" for file, name, want, got in self.misses]
        if self.failed:
            out += ["", "No valid answer: " + ", ".join(self.failed)]
        return out


def evaluate(cfg: Config, llm: LLM, directory: Path) -> Report:
    report = Report()
    for path in sorted(directory.glob("*.json")):
        label = json.loads(path.read_text(encoding="utf-8"))
        if label.get("facts") is None:
            continue
        expected = Facts.model_validate(label["facts"]).model_dump()  # a bad label fails loudly here
        report.labeled += 1
        try:
            got = extract_posting(llm, cfg, label["posting"]).model_dump()
        except (InvalidOutput, RequestRejected):
            report.failed.append(path.name)
            continue
        for name, want in expected.items():
            if name in _NOT_SCORED:
                continue
            report.total[name] += 1
            if _same(name, want, got[name]):
                report.right[name] += 1
            else:
                report.misses.append((path.name, name, want, got[name]))
        labeled_stack = {score.canonical_skill(s) for s in expected["stack"]}
        if labeled_stack:
            found = labeled_stack & {score.canonical_skill(s) for s in got["stack"]}
            report.stack_recall.append(len(found) / len(labeled_stack))
    return report


def _same(name: str, want: Any, got: Any) -> bool:
    if want is None or got is None:
        return want is None and got is None
    if name in _NUMBERS:
        return abs(float(want) - float(got)) <= 0.01 * max(abs(float(want)), 1.0)
    if name in _TEXTS:
        return str(want).strip().lower() == str(got).strip().lower()
    if name == "primary_language":
        return score.canonical_skill(want) == score.canonical_skill(got)
    if name == "overlap":
        return score.jst_window(want) == score.jst_window(got)  # the same hours, however the zone is written
    return want == got
