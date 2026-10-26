"""`ci-lint perf compare`: issue #6 §3/§9's perf suite -- compare a
baseline and a current benchmark report.

AGENTS.md's typed-benchmark rule applies directly here: benchmark inputs,
results, and reports are frozen dataclasses internally; the JSON file is a
wire boundary only, and it is fully validated and converted into
`BenchmarkRecord`/`BenchmarkFile` before any comparison logic runs -- no
`dict[str, Any]` (or bare `dict`) is ever passed between functions in this
module. `JsonRecord` types that boundary concretely (PY-001 compliant).

Non-gating unless ci.toml's `[suites.perf].gating = true` AND
`--threshold-pct` is given; either way every benchmark's delta is always
reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ci_lint.schema import CiToml

PERF_SCHEMA_VERSION = 1

# The typed wire boundary for one raw benchmark record read from a JSON
# file, per AGENTS.md ("type any boundary dictionary with concrete value
# types, never Any"). Never passed around after `load_benchmark_file`
# converts it into `BenchmarkRecord`.
JsonScalar = None | bool | int | float | str
JsonRecord = dict[str, JsonScalar | list[float]]


class PerfCompareError(Exception):
    """Malformed/missing benchmark JSON file -- exit 2."""


@dataclass(frozen=True)
class BenchmarkRecord:
    name: str
    unit: str
    samples: tuple[float, ...]
    median: float


@dataclass(frozen=True)
class BenchmarkFile:
    schema_version: int
    benchmarks: tuple[BenchmarkRecord, ...]


def _to_record(path: Path, index: int, item: object) -> BenchmarkRecord:
    if not isinstance(item, dict):
        raise PerfCompareError(f"{path}: benchmarks[{index}] is not a JSON object")
    raw: JsonRecord = item  # narrowed by the field checks below
    name = raw.get("name")
    unit = raw.get("unit")
    samples_raw = raw.get("samples")
    median = raw.get("median")
    if not isinstance(name, str) or not name:
        raise PerfCompareError(f"{path}: benchmarks[{index}].name must be a non-empty string")
    if not isinstance(unit, str) or not unit:
        raise PerfCompareError(f"{path}: benchmarks[{index}].unit must be a non-empty string")
    if not isinstance(samples_raw, list) or not samples_raw or not all(
        isinstance(s, (int, float)) and not isinstance(s, bool) for s in samples_raw
    ):
        raise PerfCompareError(f"{path}: benchmarks[{index}].samples must be a non-empty list of numbers")
    if not isinstance(median, (int, float)) or isinstance(median, bool):
        raise PerfCompareError(f"{path}: benchmarks[{index}].median must be a number")
    return BenchmarkRecord(name=name, unit=unit, samples=tuple(float(s) for s in samples_raw), median=float(median))


def load_benchmark_file(path: Path) -> BenchmarkFile:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PerfCompareError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PerfCompareError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PerfCompareError(f"{path}: top level must be a JSON object")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise PerfCompareError(f"{path}: missing integer 'schema_version'")
    raw_benchmarks = raw.get("benchmarks")
    if not isinstance(raw_benchmarks, list):
        raise PerfCompareError(f"{path}: missing 'benchmarks' array")
    records = tuple(
        _to_record(path, i, item) for i, item in enumerate(raw_benchmarks)
    )
    seen: set[str] = set()
    for r in records:
        if r.name in seen:
            raise PerfCompareError(f"{path}: duplicate benchmark name {r.name!r}")
        seen.add(r.name)
    return BenchmarkFile(schema_version=schema_version, benchmarks=records)


@dataclass(frozen=True)
class BenchmarkDelta:
    name: str
    unit: str
    baseline_median: float | None
    current_median: float | None
    delta_pct: float | None  # None: no baseline/current pair, or baseline median is 0
    regression: bool  # only ever true when gating AND a threshold is set (see compare())


def compare(baseline: BenchmarkFile, current: BenchmarkFile, threshold_pct: float | None) -> tuple[BenchmarkDelta, ...]:
    by_name_baseline = {b.name: b for b in baseline.benchmarks}
    by_name_current = {b.name: b for b in current.benchmarks}
    names = sorted(set(by_name_baseline) | set(by_name_current))
    deltas: list[BenchmarkDelta] = []
    for name in names:
        b = by_name_baseline.get(name)
        c = by_name_current.get(name)
        unit = (c or b).unit  # type: ignore[union-attr]  # at least one of b/c is not None, by construction
        if b is None or c is None:
            deltas.append(
                BenchmarkDelta(
                    name=name,
                    unit=unit,
                    baseline_median=b.median if b else None,
                    current_median=c.median if c else None,
                    delta_pct=None,
                    regression=False,
                )
            )
            continue
        delta_pct = ((c.median - b.median) / b.median * 100.0) if b.median != 0 else None
        regression = threshold_pct is not None and delta_pct is not None and delta_pct > threshold_pct
        deltas.append(
            BenchmarkDelta(
                name=name, unit=c.unit, baseline_median=b.median, current_median=c.median,
                delta_pct=delta_pct, regression=regression,
            )
        )
    return tuple(deltas)


def is_gating(ci: CiToml, threshold_pct: float | None) -> bool:
    """Non-gating unless BOTH ci.toml's `[suites.perf].gating = true` AND a
    `--threshold-pct` is actually supplied -- a threshold with gating
    unset (or perf undeclared) never fails the run; it is reported only."""

    perf_suite = ci.suites.get("perf")
    return bool(perf_suite is not None and perf_suite.gating and threshold_pct is not None)


@dataclass(frozen=True)
class PerfCompareReport:
    schema_version: int
    deltas: tuple[BenchmarkDelta, ...]
    gating: bool
    threshold_pct: float | None


def to_json_dict(report: PerfCompareReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "gating": report.gating,
        "threshold_pct": report.threshold_pct,
        "benchmarks": [
            {
                "name": d.name,
                "unit": d.unit,
                "baseline_median": d.baseline_median,
                "current_median": d.current_median,
                "delta_pct": d.delta_pct,
                "regression": d.regression,
            }
            for d in report.deltas
        ],
    }


def render_text(report: PerfCompareReport) -> str:
    lines = [
        f"ci-lint perf compare: gating={'yes' if report.gating else 'no'}"
        + (f" (threshold {report.threshold_pct}%)" if report.threshold_pct is not None else "")
    ]
    lines.append(f"{'benchmark':<28} {'unit':<8} {'baseline':>12} {'current':>12} {'delta':>10}")
    for d in report.deltas:
        baseline_s = f"{d.baseline_median:.4g}" if d.baseline_median is not None else "-"
        current_s = f"{d.current_median:.4g}" if d.current_median is not None else "-"
        if d.delta_pct is None:
            delta_s = "-"
        else:
            delta_s = f"{d.delta_pct:+.2f}%"
            if d.regression:
                delta_s += " REGRESSION"
        lines.append(f"{d.name:<28} {d.unit:<8} {baseline_s:>12} {current_s:>12} {delta_s:>10}")
    n_regressions = sum(1 for d in report.deltas if d.regression)
    lines.append("")
    lines.append(f"ci-lint perf compare: {n_regressions} regression(s) over threshold" + ("" if report.gating else " (non-gating)"))
    return "\n".join(lines)
