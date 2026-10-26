"""`ci-lint perf compare` -- ci_lint/perf.py (round-5 brief, deliverable 4).
AGENTS.md typed-benchmark rule: BenchmarkRecord/BenchmarkFile are frozen
dataclasses; the JSON file is validated + converted at the boundary, never
passed around as a raw dict.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ci_lint.perf import (
    PERF_SCHEMA_VERSION,
    PerfCompareError,
    compare,
    is_gating,
    load_benchmark_file,
)
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

REPO_GATING_OFF = FIXTURES / "runtime" / "cache" / "repo"  # [suites.perf].gating = false


def _write(directory: Path, name: str, payload: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _bench_file(**benchmarks: tuple[list[float], float]) -> dict[str, object]:
    return {
        "schema_version": PERF_SCHEMA_VERSION,
        "benchmarks": [
            {"name": name, "unit": "s", "samples": samples, "median": median}
            for name, (samples, median) in benchmarks.items()
        ],
    }


class LoadBenchmarkFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_loads_valid_file(self) -> None:
        path = _write(self.dir, "b.json", _bench_file(build=([1.0, 1.2, 1.1], 1.1)))
        bf = load_benchmark_file(path)
        self.assertEqual(1, bf.schema_version)
        self.assertEqual(1, len(bf.benchmarks))
        self.assertEqual("build", bf.benchmarks[0].name)
        self.assertEqual((1.0, 1.2, 1.1), bf.benchmarks[0].samples)
        self.assertEqual(1.1, bf.benchmarks[0].median)

    def test_missing_schema_version_raises(self) -> None:
        path = _write(self.dir, "b.json", {"benchmarks": []})
        with self.assertRaises(PerfCompareError):
            load_benchmark_file(path)

    def test_not_a_json_object_raises(self) -> None:
        path = _write(self.dir, "b.json", [1, 2, 3])
        with self.assertRaises(PerfCompareError):
            load_benchmark_file(path)

    def test_missing_benchmarks_key_raises(self) -> None:
        path = _write(self.dir, "b.json", {"schema_version": 1})
        with self.assertRaises(PerfCompareError):
            load_benchmark_file(path)

    def test_non_numeric_sample_raises(self) -> None:
        payload = _bench_file(build=([1.0, "x"], 1.0))  # type: ignore[arg-type]
        path = _write(self.dir, "b.json", payload)
        with self.assertRaises(PerfCompareError):
            load_benchmark_file(path)

    def test_duplicate_benchmark_name_raises(self) -> None:
        payload = {
            "schema_version": 1,
            "benchmarks": [
                {"name": "build", "unit": "s", "samples": [1.0], "median": 1.0},
                {"name": "build", "unit": "s", "samples": [2.0], "median": 2.0},
            ],
        }
        path = _write(self.dir, "b.json", payload)
        with self.assertRaises(PerfCompareError):
            load_benchmark_file(path)

    def test_unreadable_path_raises(self) -> None:
        with self.assertRaises(PerfCompareError):
            load_benchmark_file(self.dir / "does-not-exist.json")


class CompareTest(unittest.TestCase):
    def test_regression_detected_over_threshold(self) -> None:
        baseline = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "b.json", _bench_file(build=([1.0], 1.0))))
        current = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "c.json", _bench_file(build=([1.5], 1.5))))
        deltas = compare(baseline, current, threshold_pct=10.0)
        self.assertEqual(1, len(deltas))
        d = deltas[0]
        self.assertAlmostEqual(50.0, d.delta_pct)
        self.assertTrue(d.regression)

    def test_within_threshold_is_not_a_regression(self) -> None:
        baseline = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "b.json", _bench_file(build=([1.0], 1.0))))
        current = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "c.json", _bench_file(build=([1.05], 1.05))))
        deltas = compare(baseline, current, threshold_pct=10.0)
        self.assertFalse(deltas[0].regression)

    def test_no_threshold_never_regresses(self) -> None:
        baseline = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "b.json", _bench_file(build=([1.0], 1.0))))
        current = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "c.json", _bench_file(build=([100.0], 100.0))))
        deltas = compare(baseline, current, threshold_pct=None)
        self.assertFalse(deltas[0].regression)
        self.assertIsNotNone(deltas[0].delta_pct)

    def test_new_benchmark_has_no_delta(self) -> None:
        baseline = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "b.json", _bench_file()))
        current = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "c.json", _bench_file(build=([1.0], 1.0))))
        deltas = compare(baseline, current, threshold_pct=10.0)
        self.assertEqual(1, len(deltas))
        self.assertIsNone(deltas[0].baseline_median)
        self.assertEqual(1.0, deltas[0].current_median)
        self.assertIsNone(deltas[0].delta_pct)
        self.assertFalse(deltas[0].regression)

    def test_removed_benchmark_has_no_delta(self) -> None:
        baseline = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "b.json", _bench_file(build=([1.0], 1.0))))
        current = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "c.json", _bench_file()))
        deltas = compare(baseline, current, threshold_pct=10.0)
        self.assertEqual(1, len(deltas))
        self.assertIsNone(deltas[0].current_median)

    def test_zero_baseline_median_has_no_percent_delta(self) -> None:
        baseline = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "b.json", _bench_file(build=([0.0], 0.0))))
        current = load_benchmark_file(_write(Path(tempfile.mkdtemp()), "c.json", _bench_file(build=([1.0], 1.0))))
        deltas = compare(baseline, current, threshold_pct=10.0)
        self.assertIsNone(deltas[0].delta_pct)
        self.assertFalse(deltas[0].regression)


class GatingTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO_GATING_OFF)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def test_gating_false_in_ci_toml_never_gates(self) -> None:
        # REPO_GATING_OFF declares [suites.perf].gating = false explicitly.
        self.assertFalse(is_gating(self.ci, threshold_pct=5.0))

    def test_no_threshold_never_gates_even_if_ci_toml_says_gating(self) -> None:
        gating_ci = self.ci.suites["perf"]
        self.assertFalse(gating_ci.gating)  # sanity: this fixture is gating=false
        self.assertFalse(is_gating(self.ci, threshold_pct=None))

    def test_undeclared_perf_suite_never_gates(self) -> None:
        no_perf_ci = replace(self.ci, suites={k: v for k, v in self.ci.suites.items() if k != "perf"})
        self.assertFalse(is_gating(no_perf_ci, threshold_pct=5.0))

    def test_gating_true_and_threshold_set_gates(self) -> None:
        gating_suite = replace(self.ci.suites["perf"], gating=True)
        gating_ci = replace(self.ci, suites={**self.ci.suites, "perf": gating_suite})
        self.assertTrue(is_gating(gating_ci, threshold_pct=5.0))
