"""`ci-lint perf pr-timing`: PERF-001 from recorded PR run-history JSON
(M2-22 brief; docs/policy-general.md's "Performance rule").

docs/policy-general.md: "at least five completed ordinary PR samples in a
rolling 30-day window, with either a 75th-percentile required job
execution time over 10 minutes or a 75th-percentile required PR critical
path over 15 minutes on two successive scans." Distinct from `ci-lint perf
compare` (`ci_lint.perf`, benchmark-numbers diff): this command computes a
fleet/repo PR-timing percentile from recorded history, never a benchmark
comparison.

AGENTS.md's typed-boundary rule applies: the wire format is a JSON array of
records; every record is validated and converted into frozen dataclasses
(`PrSample`/`JobSample`) before any percentile arithmetic runs -- no raw
dict survives past `load_history_file`.

Queue vs execution: a sample's `queue_seconds` (time before execution
started) is reported but NEVER added into `critical_path_seconds` (already
execution-only, per the policy's own "queue vs execution separated"
clause) or into any job's `execution_seconds`.

"Two successive scans": this command takes one `--history` file per scan,
in chronological order. A breach on the LAST file alone is `needs_review`
(one scan is not yet confirmed); a breach on the last two files given is
`violation`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status

PR_TIMING_SCHEMA_VERSION = 1
DEFAULT_MIN_SAMPLES = 5
DEFAULT_WINDOW_DAYS = 30
DEFAULT_JOB_THRESHOLD_SECONDS = 10 * 60.0
DEFAULT_CRITICAL_PATH_THRESHOLD_SECONDS = 15 * 60.0

JsonScalar = None | bool | int | float | str
JsonRecord = dict[str, JsonScalar | list["JsonRecord"]]


class PrTimingError(Exception):
    """Malformed/missing history JSON -- exit 2."""


@dataclass(frozen=True)
class JobSample:
    name: str
    required: bool
    execution_seconds: float


@dataclass(frozen=True)
class PrSample:
    pr_number: int
    ordinary: bool
    recorded_at: datetime
    queue_seconds: float
    critical_path_seconds: float
    jobs: tuple[JobSample, ...]


@dataclass(frozen=True)
class HistoryFile:
    schema_version: int
    samples: tuple[PrSample, ...]


@dataclass(frozen=True)
class ScanResult:
    source: str
    total_samples: int
    windowed_samples: int
    p75_job_execution_seconds: float | None
    p75_critical_path_seconds: float | None
    job_breach: bool
    critical_path_breach: bool
    insufficient_samples: bool


@dataclass(frozen=True)
class PrTimingReport:
    scans: tuple[ScanResult, ...]
    findings: tuple[Finding, ...]


def _parse_dt(path: Path, index: int, raw: object) -> datetime:
    if not isinstance(raw, str):
        raise PrTimingError(f"{path}: samples[{index}].recorded_at must be an ISO-8601 string")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrTimingError(f"{path}: samples[{index}].recorded_at is not valid ISO-8601: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_job(path: Path, sample_index: int, job_index: int, raw: object) -> JobSample:
    if not isinstance(raw, dict):
        raise PrTimingError(f"{path}: samples[{sample_index}].jobs[{job_index}] is not an object")
    name = raw.get("name")
    required = raw.get("required")
    exec_s = raw.get("execution_seconds")
    if not isinstance(name, str) or not name:
        raise PrTimingError(f"{path}: samples[{sample_index}].jobs[{job_index}].name must be a non-empty string")
    if not isinstance(required, bool):
        raise PrTimingError(f"{path}: samples[{sample_index}].jobs[{job_index}].required must be a bool")
    if not isinstance(exec_s, (int, float)) or isinstance(exec_s, bool):
        raise PrTimingError(
            f"{path}: samples[{sample_index}].jobs[{job_index}].execution_seconds must be a number"
        )
    return JobSample(name=name, required=required, execution_seconds=float(exec_s))


def _to_sample(path: Path, index: int, raw: object) -> PrSample:
    if not isinstance(raw, dict):
        raise PrTimingError(f"{path}: samples[{index}] is not an object")
    pr_number = raw.get("pr_number")
    ordinary = raw.get("ordinary")
    queue_s = raw.get("queue_seconds")
    critical_path_s = raw.get("critical_path_seconds")
    jobs_raw = raw.get("jobs")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        raise PrTimingError(f"{path}: samples[{index}].pr_number must be an integer")
    if not isinstance(ordinary, bool):
        raise PrTimingError(f"{path}: samples[{index}].ordinary must be a bool")
    if not isinstance(queue_s, (int, float)) or isinstance(queue_s, bool):
        raise PrTimingError(f"{path}: samples[{index}].queue_seconds must be a number")
    if not isinstance(critical_path_s, (int, float)) or isinstance(critical_path_s, bool):
        raise PrTimingError(f"{path}: samples[{index}].critical_path_seconds must be a number")
    if not isinstance(jobs_raw, list):
        raise PrTimingError(f"{path}: samples[{index}].jobs must be an array")
    jobs = tuple(_to_job(path, index, j, item) for j, item in enumerate(jobs_raw))
    return PrSample(
        pr_number=pr_number,
        ordinary=ordinary,
        recorded_at=_parse_dt(path, index, raw.get("recorded_at")),
        queue_seconds=float(queue_s),
        critical_path_seconds=float(critical_path_s),
        jobs=jobs,
    )


def load_history_file(path: Path) -> HistoryFile:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrTimingError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PrTimingError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PrTimingError(f"{path}: top level must be a JSON object")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise PrTimingError(f"{path}: missing integer 'schema_version'")
    samples_raw = raw.get("samples")
    if not isinstance(samples_raw, list):
        raise PrTimingError(f"{path}: missing 'samples' array")
    samples = tuple(_to_sample(path, i, item) for i, item in enumerate(samples_raw))
    return HistoryFile(schema_version=schema_version, samples=samples)


def _percentile_75(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(0.75 * len(ordered)) - 1)
    return ordered[idx]


def _scan_one(
    source: str,
    hist: HistoryFile,
    *,
    min_samples: int,
    window_days: int,
    job_threshold_seconds: float,
    critical_path_threshold_seconds: float,
) -> ScanResult:
    ordinary = [s for s in hist.samples if s.ordinary]
    if not ordinary:
        return ScanResult(source, 0, 0, None, None, False, False, True)
    reference = max(s.recorded_at for s in ordinary)
    window_start = reference - timedelta(days=window_days)
    windowed = [s for s in ordinary if s.recorded_at >= window_start]

    if len(windowed) < min_samples:
        return ScanResult(source, len(ordinary), len(windowed), None, None, False, False, True)

    job_values = [j.execution_seconds for s in windowed for j in s.jobs if j.required]
    critical_values = [s.critical_path_seconds for s in windowed]
    p75_job = _percentile_75(job_values)
    p75_crit = _percentile_75(critical_values)
    job_breach = p75_job is not None and p75_job > job_threshold_seconds
    crit_breach = p75_crit is not None and p75_crit > critical_path_threshold_seconds
    return ScanResult(source, len(ordinary), len(windowed), p75_job, p75_crit, job_breach, crit_breach, False)


@dataclass(frozen=True)
class HistoryScan:
    """One `--history` file: its source path and the parsed history."""

    source: str
    history: HistoryFile


def compute_pr_timing(
    scans: list[HistoryScan],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    window_days: int = DEFAULT_WINDOW_DAYS,
    job_threshold_seconds: float = DEFAULT_JOB_THRESHOLD_SECONDS,
    critical_path_threshold_seconds: float = DEFAULT_CRITICAL_PATH_THRESHOLD_SECONDS,
) -> PrTimingReport:
    results = [
        _scan_one(
            source,
            hist,
            min_samples=min_samples,
            window_days=window_days,
            job_threshold_seconds=job_threshold_seconds,
            critical_path_threshold_seconds=critical_path_threshold_seconds,
        )
        for source, hist in ((sc.source, sc.history) for sc in scans)
    ]

    findings: list[Finding] = []
    for r in results:
        if r.insufficient_samples:
            findings.append(
                Finding(
                    rule="PERF-001",
                    path=r.source,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"only {r.windowed_samples} ordinary PR sample(s) in the last {window_days} "
                        f"days (need >= {min_samples}) -- not enough evidence to evaluate the p75 "
                        "timing thresholds yet"
                    ),
                    fix="record more completed ordinary-PR run samples (queue/execution/critical-path "
                    "seconds per required job) into this history file until the rolling window has at "
                    f"least {min_samples} samples",
                )
            )

    breaching = [r for r in results if not r.insufficient_samples and (r.job_breach or r.critical_path_breach)]
    if breaching:
        last = results[-1]
        last_breaches = not last.insufficient_samples and (last.job_breach or last.critical_path_breach)
        two_successive = len(results) >= 2 and last_breaches and (
            not results[-2].insufficient_samples and (results[-2].job_breach or results[-2].critical_path_breach)
        )
        for r in breaching:
            what: list[str] = []
            if r.job_breach:
                what.append(f"p75 required-job execution {r.p75_job_execution_seconds:.0f}s > {job_threshold_seconds:.0f}s")
            if r.critical_path_breach:
                what.append(f"p75 critical path {r.p75_critical_path_seconds:.0f}s > {critical_path_threshold_seconds:.0f}s")
            status = Status.VIOLATION if (r is last and two_successive) else Status.NEEDS_REVIEW
            note = (
                "confirmed on two successive scans"
                if status == Status.VIOLATION
                else "not yet confirmed on a second successive scan"
            )
            findings.append(
                Finding(
                    rule="PERF-001",
                    path=r.source,
                    status=status,
                    message=f"PR timing breach ({', '.join(what)}) over {r.windowed_samples} samples -- {note}",
                    fix="investigate and reduce the required job(s)'/critical path's execution time "
                    "(cache warmth, matrix fan-out, unnecessary required jobs); queue time is reported "
                    "separately and is not itself part of this threshold",
                )
            )

    return PrTimingReport(scans=tuple(results), findings=tuple(findings))


def to_json_dict(report: PrTimingReport) -> dict[str, JsonValue]:
    return {
        "scans": [
            {
                "source": r.source,
                "total_samples": r.total_samples,
                "windowed_samples": r.windowed_samples,
                "p75_job_execution_seconds": r.p75_job_execution_seconds,
                "p75_critical_path_seconds": r.p75_critical_path_seconds,
                "job_breach": r.job_breach,
                "critical_path_breach": r.critical_path_breach,
                "insufficient_samples": r.insufficient_samples,
            }
            for r in report.scans
        ],
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: PrTimingReport) -> str:
    lines = [f"ci-lint perf pr-timing: {len(report.scans)} scan(s)"]
    for r in report.scans:
        lines.append(
            f"  {r.source}: {r.windowed_samples}/{r.total_samples} windowed samples, "
            f"p75 job={r.p75_job_execution_seconds}, p75 critical_path={r.p75_critical_path_seconds}"
        )
    if not report.findings:
        lines.append("no findings.")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint perf pr-timing: {len(report.findings)} finding(s)")
    return "\n".join(lines)
