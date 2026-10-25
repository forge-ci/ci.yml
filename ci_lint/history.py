"""`ci-lint history`: diff two or more recorded `fleet scan --json` files
(zackees/ci.yml#99).

Pure filesystem, standard library only, no GitHub call. Every finding is
keyed exactly as `ci-lint sync-issues` keys its issues -- (repo, rule,
subject), subject = `sync_issues.subject_of` -- so a `history` line and a
fingerprinted issue name the same thing. Only `violation` and
`needs_review` findings are tracked (a scan emits nothing else today).

Each scan file is dated by its top-level `scanned_at` field (written by
`fleet scan --json` since #99) or, for older recordings, a `YYYY-MM-DD` in
its filename. Scans are ordered by that date (then path) and the newest is
compared with the one before it:

  - new:        in the newest scan, not in the previous one
  - persisting: in both; `first_seen` is the start of its unbroken streak
  - resolved:   in the previous scan, gone from the newest -- but only when
                the newest scan covered that repository; a repository the
                newest scan did not include is `unscanned`, never resolved
                (a narrowed `--repos` run proves nothing about the others).

Output is sorted (repo, rule, subject) so the same inputs always print the
same bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Status
from ci_lint.fleet import FleetReport, load_report_json
from ci_lint.sync_issues import subject_of

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TRACKED = (Status.VIOLATION, Status.NEEDS_REVIEW)
STATES = ("new", "persisting", "resolved", "unscanned")


@dataclass(frozen=True)
class Snapshot:
    date: str
    path: str
    report: FleetReport


@dataclass(frozen=True)
class FindingKey:
    repo: str
    rule: str
    subject: str


@dataclass(frozen=True)
class Observed:
    status: str
    message: str


@dataclass(frozen=True)
class ObservedFinding:
    key: FindingKey
    observed: Observed


@dataclass(frozen=True)
class HistoryEntry:
    repo: str
    rule: str
    subject: str
    state: str
    status: str
    first_seen: str
    last_seen: str
    message: str


@dataclass(frozen=True)
class HistoryReport:
    scans: tuple[str, ...]  # "<date> <path>", oldest first
    entries: tuple[HistoryEntry, ...]


def scan_date(path: Path, data: object) -> str:
    """The scan's YYYY-MM-DD: `scanned_at` first, then the filename."""

    if isinstance(data, dict):
        stamp = data.get("scanned_at")
        if isinstance(stamp, str):
            match = _DATE_RE.match(stamp)
            if match:
                return match.group(1)
    match = _DATE_RE.search(path.name)
    if match:
        return match.group(1)
    raise ValueError(f"{path}: no `scanned_at` field and no YYYY-MM-DD in the filename")


def load_snapshot(path: Path) -> Snapshot:
    text = path.read_text(encoding="utf-8")
    return Snapshot(date=scan_date(path, json.loads(text)), path=str(path), report=load_report_json(text))


def _observed(snap: Snapshot) -> tuple[ObservedFinding, ...]:
    out = {}
    for scan in snap.report.repos:
        for f in scan.findings:
            if f.status not in _TRACKED:
                continue
            key = FindingKey(scan.repo, f.rule, subject_of(f, scan.repo))
            prior = out.get(key)
            # A violation outranks needs_review for the same key.
            if prior is None or (prior.status != Status.VIOLATION.value and f.status == Status.VIOLATION):
                out[key] = Observed(f.status.value, f.message)
    return tuple(ObservedFinding(k, v) for k, v in out.items())


def _streak_start(key: FindingKey, upto: int, seen: list[dict], snaps: list[Snapshot]) -> str:  # type: ignore[type-arg]
    i = upto
    while i > 0 and key in seen[i - 1]:
        i -= 1
    return snaps[i].date


def build_history(snapshots: list[Snapshot]) -> HistoryReport:
    if len(snapshots) < 2:
        raise ValueError("history needs at least two scans")
    snaps = sorted(snapshots, key=lambda s: (s.date, s.path))
    seen = [{o.key: o.observed for o in _observed(s)} for s in snaps]
    last, prev = len(snaps) - 1, len(snaps) - 2
    covered = {r.repo for r in snaps[last].report.repos}
    entries: list[HistoryEntry] = []
    for key in set(seen[last]) | set(seen[prev]):
        if key in seen[last]:
            obs = seen[last][key]
            state = "persisting" if key in seen[prev] else "new"
            first, last_seen = _streak_start(key, last, seen, snaps), snaps[last].date
        else:
            obs = seen[prev][key]
            state = "resolved" if key.repo in covered else "unscanned"
            first, last_seen = _streak_start(key, prev, seen, snaps), snaps[prev].date
        entries.append(
            HistoryEntry(key.repo, key.rule, key.subject, state, obs.status, first, last_seen, obs.message)
        )
    entries.sort(key=lambda e: (e.repo, e.rule, e.subject))
    return HistoryReport(scans=tuple(f"{s.date} {s.path}" for s in snaps), entries=tuple(entries))


def counts(report: HistoryReport) -> dict[str, JsonValue]:
    return {state: sum(1 for e in report.entries if e.state == state) for state in STATES}


def to_json_dict(report: HistoryReport) -> JsonValue:
    return {
        "scans": list(report.scans),
        "counts": counts(report),
        "entries": [
            {
                "repo": e.repo,
                "rule": e.rule,
                "subject": e.subject,
                "state": e.state,
                "status": e.status,
                "first_seen": e.first_seen,
                "last_seen": e.last_seen,
                "message": e.message,
            }
            for e in report.entries
        ],
    }


def render_text(report: HistoryReport) -> str:
    c = counts(report)
    lines = [
        f"ci-lint history: {len(report.scans)} scans, newest vs previous -- "
        + ", ".join(f"{c[s]} {s}" for s in STATES),
        *(f"  scan {s}" for s in report.scans),
    ]
    for state in STATES:
        rows = [e for e in report.entries if e.state == state]
        if not rows:
            continue
        lines.extend(["", f"## {state}"])
        lines.extend(
            f"  {e.repo} {e.rule} {e.subject} [{e.status}] first seen {e.first_seen}, last seen {e.last_seen}"
            for e in rows
        )
    return "\n".join(lines)
