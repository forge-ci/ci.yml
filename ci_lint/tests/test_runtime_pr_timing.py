"""PERF-001 -- ci_lint/runtime/pr_timing.py (M2-22, `ci-lint perf
pr-timing`): PR timing breach from recorded run-history JSON, sample/
threshold rule from docs/policy-general.md."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.runtime.pr_timing import (
    PR_TIMING_SCHEMA_VERSION,
    HistoryScan,
    PrTimingError,
    compute_pr_timing,
    load_history_file,
)


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]


def _sample(pr: int, recorded_at: str, *, ordinary: bool = True, queue: float = 30.0, crit: float = 400.0, job_exec: float = 400.0) -> dict:
    return {
        "pr_number": pr,
        "ordinary": ordinary,
        "recorded_at": recorded_at,
        "queue_seconds": queue,
        "critical_path_seconds": crit,
        "jobs": [{"name": "fast", "required": True, "execution_seconds": job_exec}],
    }


def _history(samples: list[dict]) -> dict:
    return {"schema_version": PR_TIMING_SCHEMA_VERSION, "samples": samples}


class PrTimingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name: str, payload: dict) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_insufficient_samples_is_needs_review(self) -> None:
        samples = [_sample(i, "2026-09-01T00:00:00Z") for i in range(3)]
        path = self._write("scan1.json", _history(samples))
        report = compute_pr_timing([HistoryScan(str(path), load_history_file(path))])
        self.assertIn("PERF-001", rule_ids(report.findings))
        self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in report.findings))

    def test_below_threshold_is_clean(self) -> None:
        samples = [_sample(i, "2026-09-01T00:00:00Z", crit=300.0, job_exec=300.0) for i in range(6)]
        path = self._write("scan1.json", _history(samples))
        report = compute_pr_timing([HistoryScan(str(path), load_history_file(path))])
        self.assertEqual([], list(report.findings))

    def test_single_scan_breach_is_needs_review(self) -> None:
        samples = [_sample(i, "2026-09-01T00:00:00Z", crit=1200.0, job_exec=1200.0) for i in range(6)]
        path = self._write("scan1.json", _history(samples))
        report = compute_pr_timing([HistoryScan(str(path), load_history_file(path))])
        self.assertIn("PERF-001", rule_ids(report.findings))
        self.assertTrue(any(f.status == Status.NEEDS_REVIEW for f in report.findings))
        self.assertTrue(any("not yet confirmed" in f.message for f in report.findings))

    def test_two_successive_scan_breach_is_violation(self) -> None:
        samples = [_sample(i, "2026-09-01T00:00:00Z", crit=1200.0, job_exec=1200.0) for i in range(6)]
        path1 = self._write("scan1.json", _history(samples))
        path2 = self._write("scan2.json", _history(samples))
        report = compute_pr_timing(
            [HistoryScan(str(path1), load_history_file(path1)), HistoryScan(str(path2), load_history_file(path2))]
        )
        self.assertIn("PERF-001", rule_ids(report.findings))
        last_scan_findings = [f for f in report.findings if f.path == str(path2)]
        self.assertTrue(any(f.status == Status.VIOLATION for f in last_scan_findings))

    def test_non_ordinary_samples_excluded(self) -> None:
        samples = [_sample(i, "2026-09-01T00:00:00Z", ordinary=False, crit=1200.0) for i in range(6)]
        path = self._write("scan1.json", _history(samples))
        report = compute_pr_timing([HistoryScan(str(path), load_history_file(path))])
        # zero ordinary samples -> insufficient, not a breach
        self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in report.findings))

    def test_queue_seconds_not_counted_toward_critical_path(self) -> None:
        samples = [
            _sample(i, "2026-09-01T00:00:00Z", queue=10_000.0, crit=300.0, job_exec=300.0) for i in range(6)
        ]
        path = self._write("scan1.json", _history(samples))
        report = compute_pr_timing([HistoryScan(str(path), load_history_file(path))])
        self.assertEqual([], list(report.findings))

    def test_malformed_history_raises(self) -> None:
        path = self._write("bad.json", {"schema_version": 1})
        with self.assertRaises(PrTimingError):
            load_history_file(path)


if __name__ == "__main__":
    unittest.main()
