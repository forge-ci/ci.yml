"""`ci-lint cache hit-rate`: GEN-023's log-evidence clauses (zackees/ci.yml#411).

The static half (`ci_lint.rules.cache_stats_summary`) proves a job is wired
to publish compiler-cache stats. This half reads what a job actually
printed. The input is a job log (`gh run view <run> --log` or
`--job <id> --log`, or captured stdout); ci-lint never fetches it.

Evidence shape (setup-soldr origin/main 28003e9 / soldr, verbatim from
forge-ci/zccache run 40260098536, ANSI colour stripped):

    soldr: cache 0 HIT, 392 MISS (0% hit rate, saved 0.0s)

and zackees/clud#2037's `soldr: cache 381 HIT, 188 MISS (67% hit rate)` on
a PR whose tree matched `main`. Every line is one soldr session; a job's
sessions are summed.

Findings are always `needs_review`, never a violation: a log cannot say
whether the run was warm (a lockfile bump, a new toolchain, or a first run
legitimately misses), so a low rate is a prompt to run `cache save-check`
/ `cache rework`, not a failure.

- a job whose log shows cargo compiling (`Compiling <crate> v<ver>`) but no
  `soldr: cache` stats line -- the summary GEN-023 requires is missing;
- a job whose summed sessions reach `min_compilations` (default 50) with a
  hit rate below `threshold` (default 0.80). Below 50 compilations the rate
  is reported, not judged.

The exact-vs-fallback restore flag is not in any job log setup-soldr
writes today (only the `build-cache-restore-status` output), so it is not
checked here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status

RULE = "GEN-023"
DEFAULT_THRESHOLD = 0.80
DEFAULT_MIN_COMPILATIONS = 50

_ANSI = re.compile(r"\x1b\[[0-9;]*m|\^\[\[[0-9;]*m")
_TS = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_SESSION = re.compile(r"soldr: cache (?P<hits>\d+) HIT, (?P<misses>\d+) MISS\b")
_COMPILING = re.compile(r"^\s*Compiling [\w.-]+ v\d")


class HitRateError(Exception):
    """A `--log` file could not be read."""


@dataclass(frozen=True)
class JobStats:
    job: str
    source: str
    sessions: int
    hits: int
    misses: int
    compiling: bool

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def rate(self) -> float | None:
        return self.hits / self.total if self.total else None


@dataclass(frozen=True)
class LogLine:
    job: str
    text: str


@dataclass(frozen=True)
class HitRateReport:
    jobs: tuple[JobStats, ...]
    findings: tuple[Finding, ...]


def _split(raw: str, source: str) -> LogLine:
    """`gh run view --log` prefixes `<job>\\t<step>\\t`; a single-job log has none."""

    parts = raw.split("\t", 2)
    job, text = (parts[0], parts[2]) if len(parts) == 3 else (source, raw)
    return LogLine(job, _TS.sub("", _ANSI.sub("", text)))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HitRateError(f"cannot read --log {path}: {exc}") from exc


@dataclass
class _Acc:
    sessions: int = 0
    hits: int = 0
    misses: int = 0
    compiling: bool = False


def parse_jobs(log_paths: list[Path]) -> tuple[JobStats, ...]:
    out: list[JobStats] = []
    for path in log_paths:
        accs: dict[str, _Acc] = {}
        for raw in _read(path).splitlines():
            line = _split(raw, str(path))
            acc = accs.setdefault(line.job, _Acc())
            m = _SESSION.search(line.text)
            if m:
                acc.sessions += 1
                acc.hits += int(m.group("hits"))
                acc.misses += int(m.group("misses"))
            elif _COMPILING.match(line.text):
                acc.compiling = True
        for job, a in accs.items():
            if a.sessions or a.compiling:
                out.append(JobStats(job, str(path), a.sessions, a.hits, a.misses, a.compiling))
    return tuple(out)


def _finding(js: JobStats, message: str, fix: str) -> Finding:
    return Finding(
        rule=RULE, path=f"cache:log:{js.source}", status=Status.NEEDS_REVIEW, message=f"{js.job}: {message}", fix=fix
    )


def check_hit_rate(
    log_paths: list[Path],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_compilations: int = DEFAULT_MIN_COMPILATIONS,
) -> HitRateReport:
    jobs = parse_jobs(log_paths)
    findings: list[Finding] = []
    for js in jobs:
        if js.sessions == 0:
            findings.append(
                _finding(
                    js,
                    "compiled Rust crates but printed no 'soldr: cache N HIT, M MISS' compile-cache stats line",
                    "route the build through setup-soldr with compile-cache-stats left at its 'summarize' default "
                    "(or through soldr) so hits/misses reach the log and $GITHUB_STEP_SUMMARY",
                )
            )
            continue
        rate = js.rate
        if rate is not None and js.total >= min_compilations and rate < threshold:
            findings.append(
                _finding(
                    js,
                    f"compile-cache hit rate {rate:.0%} ({js.hits} HIT / {js.misses} MISS over {js.sessions} "
                    f"session(s)) is below {threshold:.0%}",
                    "if this run should have been warm (tree matches a default-branch writer), check the "
                    "restore key (exact vs fallback, build-cache-restore-status), then run 'ci-lint cache "
                    "save-check' on the writer and 'ci-lint cache rework' on this log; a lockfile/toolchain "
                    "change or a first run explains a cold cache",
                )
            )
    return HitRateReport(jobs, tuple(findings))


def to_json_dict(report: HitRateReport) -> dict[str, JsonValue]:
    return {
        "jobs": [
            {
                "job": j.job,
                "source": j.source,
                "sessions": j.sessions,
                "hits": j.hits,
                "misses": j.misses,
                "hit_rate": j.rate,
                "compiling": j.compiling,
            }
            for j in report.jobs
        ],
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: HitRateReport) -> str:
    lines = [f"ci-lint cache hit-rate: {len(report.jobs)} compiling job(s)"]
    for j in report.jobs:
        rate = "n/a" if j.rate is None else f"{j.rate:.0%}"
        lines.append(f"  {j.job}: {j.hits} HIT / {j.misses} MISS ({rate}, {j.sessions} session(s))")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint cache hit-rate: {len(report.findings)} needs_review finding(s)")
    return "\n".join(lines)
