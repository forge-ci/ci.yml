"""`ci-lint cache save-check`: CACHE-011/CACHE-012/CACHE-026 from real job-log
evidence (issue #7).

CACHE-010 (`ci_lint.rules.cache_static.check_cache_010`) is the static
half: a kill-switch env var contradicting a declared cache family. This
module is the runtime half -- neither "a green default-branch run saved
nothing" nor "a restore keeps hitting a poisoned/empty archive" is visible
from YAML alone; both need a real job log, matching the
`ci_lint.runtime.dylint`/`ci_lint.runtime.suite` convention of taking
`--log` evidence rather than fetching it live (RUST-003/TEST-001/002's own
precedent -- `gh run view --job <id> --log`, or any captured stdout of the
same, is the input; ci-lint never assumes network access to fetch a job
log itself).

Mechanical signals (issue #7's own evidence, zackees/clud run
`36463271709` job `109067163056`):

  CACHE-011: a run whose CONCLUSION is `success` (the caller supplies this
  -- ci-lint has no way to infer it from a log body alone) but whose log
  shows a declared layer's save was skipped for a reason other than an
  exact hit --
    `<layer>: ... skipping save` (e.g. "dylint-cache: no matching
    successful Dylint marker - skipping save", "dylint-output-cache:
    Dylint did not complete successfully - skipping save"),
    `final <layer> session stats: missing`, or
    `saved id=-1` in a `final ... summary` line.

  CACHE-012: a family's newest default-branch entry gets restored as an
  "unusable payload" (0 extracted files/bytes) on >= `threshold`
  (default 2) CONSECUTIVE runs' logs, with no successful save of that
  layer recorded in between -- the steady-state detector for a writer
  that never heals itself (pairs with `ci_lint.cache.ops.heal`, which
  clears the symptom but not the cause).

  CACHE-026: a cache guard disabled writes (`cache writes disabled ...`,
  `*CACHE_WRITES=false`, `cache_writes=false`) on >= `guard_threshold`
  (default 3) CONSECUTIVE default-branch runs' logs -- a silent permanent
  freeze rather than a time-boxed, self-healing refusal (forge-ci/zccache
  2026-10-22..26).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding

# "<layer>: <free text> skipping save" -- covers both clud log lines
# verbatim ("no matching successful Dylint marker - skipping save" and
# "Dylint did not complete successfully - skipping save") without hardcoding
# either exact reason string, since setup-soldr/zccache may phrase a third
# skip reason the same way.
_SKIP_SAVE_RE = re.compile(r"^(?P<layer>[A-Za-z][\w.-]*):\s*(?P<reason>[^\n]*\bskipping save\b[^\n]*)$", re.MULTILINE)

# "final <layer> session stats: missing" -- zccache's own summary line when
# the layer never ran at all (issue #7's kill-switch evidence: "final
# zccache session stats: missing").
_SESSION_MISSING_RE = re.compile(r"^final (?P<layer>[\w.-]+) session stats:\s*missing\s*$", re.MULTILINE)

# "final cache summary: ... build=miss/saved id=-1" -- setup-soldr's own
# summary line when a save never happened. No layer name is embedded in
# this particular line (issue #7's evidence quotes it bare), so the event
# carries an empty layer and CACHE-011 reports it unqualified.
_SAVED_MISS_RE = re.compile(r"^(?P<summary>final [\w.-]+ summary:.*\bsaved id=-1\b.*)$", re.MULTILINE)

# "<layer>: ... produced an unusable payload: archive=22B extracted_files=0
# extracted_bytes=0" -- a restore that decoded to nothing.
_UNUSABLE_RE = re.compile(
    r"^(?P<layer>[A-Za-z][\w.-]*):.*produced an unusable payload: "
    r"archive=(?P<archive>\d+)B extracted_files=(?P<files>\d+) extracted_bytes=(?P<bytes>\d+)",
    re.MULTILINE,
)

DEFAULT_UNUSABLE_STREAK_THRESHOLD = 2

# CACHE-026: a cache guard (barrier/budget/pre-prune) that turned cache
# writes off for the run. zccache's wait-cache-pre-prune emits
# `##[warning]cache writes disabled for this job: <reason>` and exports
# `ZCCACHE_CACHE_WRITES=false`; a `cache_writes=false` step output is the
# policy's own sanctioned shape. Any of the three counts as one event per log.
_GUARD_DISABLED_RE = re.compile(
    r"(?:^(?:##\[warning\]|Warning:\s*)?(?P<reason>cache[ _-]writes?\b[^\n]*\bdisabled\b[^\n]*)$"
    r"|(?P<flag>\b[A-Z0-9_]*CACHE_WRITES=false\b|\bcache_writes=false\b))",
    re.MULTILINE | re.IGNORECASE,
)

# Consecutive default-branch runs with writes guard-disabled before the
# freeze stops being a transient refusal (zccache: every main push for 4+
# days, 2026-10-22..26, with every PR restoring the frozen lock).
DEFAULT_GUARD_FREEZE_THRESHOLD = 3

# GitHub CLI logs have two tab-delimited job/step fields before their
# ISO timestamp; setup-soldr adds an elapsed MM:SS prefix to its messages.
# Remove only those anchored wrappers, preserving the layer/reason payload.
_GITHUB_PREFIX_RE = re.compile(
    r"^[^\t\n]+\t[^\t\n]+\t(?=\d{4}-\d{2}-\d{2}T[\d:.]+Z )", re.MULTILINE
)
_TIMESTAMP_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ", re.MULTILINE)
_ELAPSED_PREFIX_RE = re.compile(r"^\d{2,}:\d{2}(?::\d{2})? ", re.MULTILINE)


def _normalize_log_prefixes(text: str) -> str:
    text = _GITHUB_PREFIX_RE.sub("", text)
    text = _TIMESTAMP_PREFIX_RE.sub("", text)
    return _ELAPSED_PREFIX_RE.sub("", text)


class SaveEvidenceError(Exception):
    """An unreadable `--log` file -- exit 2, never silently "no evidence"
    (matching `DylintCoverageError`'s convention: a tooling failure must
    never look identical to the violation it exists to catch)."""


@dataclass(frozen=True)
class SaveSkipEvent:
    source: str
    layer: str  # "" when the log line names no layer (the bare summary form)
    reason: str


@dataclass(frozen=True)
class UnusableRestoreEvent:
    source: str
    layer: str
    archive_bytes: int


@dataclass(frozen=True)
class Cache011Result:
    """CACHE-011's skipped-save events and the findings they raise.

    Iterable so `events, findings = check_cache_011(...)` keeps working
    (PY-002, #193)."""

    events: tuple[SaveSkipEvent, ...]
    findings: tuple[Finding, ...]

    def __iter__(self) -> Iterator[tuple[SaveSkipEvent, ...] | tuple[Finding, ...]]:
        return iter((self.events, self.findings))


@dataclass(frozen=True)
class Cache012Result:
    """CACHE-012's unusable-restore events and the findings they raise
    (iterable like `Cache011Result`)."""

    events: tuple[UnusableRestoreEvent, ...]
    findings: tuple[Finding, ...]

    def __iter__(self) -> Iterator[tuple[UnusableRestoreEvent, ...] | tuple[Finding, ...]]:
        return iter((self.events, self.findings))


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SaveEvidenceError(f"cannot read --log {path}: {exc}") from exc
    return _normalize_log_prefixes(text)


def check_cache_011(
    log_paths: list[Path], *, conclusion: str, run_url: str | None = None
) -> Cache011Result:
    """`conclusion` is the run/job's ACTUAL GitHub conclusion, supplied by
    the caller (e.g. `gh run view --json conclusion`) -- CACHE-011 only
    ever fires when it is exactly `"success"`. A failed or cancelled build
    legitimately skips saving; that is not issue #7's silent failure mode
    ("required checks stay green ... and the checker's job is detection
    with log evidence")."""

    if conclusion != "success":
        return Cache011Result((), ())

    events: list[SaveSkipEvent] = []
    for path in log_paths:
        text = _read(path)
        for m in _SKIP_SAVE_RE.finditer(text):
            reason = m.group("reason").strip()
            if "exact hit" in reason.lower():
                continue  # an exact-hit skip is a healthy warm cache, not a missed save
            events.append(SaveSkipEvent(source=str(path), layer=m.group("layer"), reason=reason))
        for m in _SESSION_MISSING_RE.finditer(text):
            events.append(
                SaveSkipEvent(source=str(path), layer=m.group("layer"), reason="final session stats: missing")
            )
        for m in _SAVED_MISS_RE.finditer(text):
            events.append(SaveSkipEvent(source=str(path), layer="", reason=m.group("summary").strip()))

    findings: list[Finding] = []
    where = f" ({run_url})" if run_url else ""
    for ev in events:
        layer_desc = repr(ev.layer) if ev.layer else "(unnamed layer)"
        findings.append(
            Finding(
                rule="CACHE-011",
                path=f"cache:log:{ev.source}",
                message=f"successful default-branch run{where} saved nothing for layer {layer_desc}: {ev.reason}",
                fix="inspect the writer job's cache-save gate for this layer (setup-soldr's success-marker "
                "identity check -- see RUST-004/setup-soldr#538 -- or the soldr invocation path that "
                "should write it): a job conclusion of 'success' with a skipped/missing/failed save means "
                "the writer flow itself is broken, not that there was nothing new to save (issue #7)",
            )
        )
    return Cache011Result(tuple(events), tuple(findings))


def check_cache_012(
    log_paths: list[Path], *, threshold: int = DEFAULT_UNUSABLE_STREAK_THRESHOLD
) -> Cache012Result:
    """`log_paths` must be given OLDEST-FIRST (successive runs' logs against
    the same repository). A layer whose restore reports an unusable
    (0-extracted) payload on `threshold` or more CONSECUTIVE logs never
    heals itself -- `ci_lint.cache.ops.heal` clears one poisoned entry, but
    if the writer that should have replaced it is still broken, the next
    save re-poisons it, so this pairs with a CACHE-011 finding for the same
    layer rather than replacing it."""

    per_layer: dict[str, list[UnusableRestoreEvent]] = {}
    for path in log_paths:
        text = _read(path)
        for m in _UNUSABLE_RE.finditer(text):
            layer = m.group("layer")
            per_layer.setdefault(layer, []).append(
                UnusableRestoreEvent(source=str(path), layer=layer, archive_bytes=int(m.group("archive")))
            )

    events: list[UnusableRestoreEvent] = []
    findings: list[Finding] = []
    for layer in sorted(per_layer):
        evs = per_layer[layer]
        events.extend(evs)
        if len(evs) < threshold:
            continue
        sources = ", ".join(e.source for e in evs)
        findings.append(
            Finding(
                rule="CACHE-012",
                path=f"cache:log:{layer}",
                message=f"layer {layer!r} restored an unusable (0-byte-payload) archive on {len(evs)} "
                f"run(s) with no writer save of that layer recorded in between ({sources})",
                fix="delete the poisoned entry (ci-lint cache heal --key <key>) AND fix the writer flow "
                "that never replaces it -- check for a matching CACHE-011 finding for this layer; heal "
                "alone only clears the symptom until the same broken writer poisons it again",
            )
        )
    return Cache012Result(tuple(events), tuple(findings))


@dataclass(frozen=True)
class GuardDisabledEvent:
    source: str
    reason: str


@dataclass(frozen=True)
class Cache026Result:
    events: tuple[GuardDisabledEvent, ...]
    findings: tuple[Finding, ...]


def _guard_event(path: Path) -> GuardDisabledEvent | None:
    m = _GUARD_DISABLED_RE.search(_read(path))
    if m is None:
        return None
    return GuardDisabledEvent(source=str(path), reason=(m.group("reason") or m.group("flag")).strip())


def _longest_streak(per_log: list[GuardDisabledEvent | None]) -> list[GuardDisabledEvent]:
    best: list[GuardDisabledEvent] = []
    cur: list[GuardDisabledEvent] = []
    for ev in per_log:
        cur = [*cur, ev] if ev is not None else []
        if len(cur) > len(best):
            best = cur
    return best


def check_cache_026(
    log_paths: list[Path], *, threshold: int = DEFAULT_GUARD_FREEZE_THRESHOLD
) -> Cache026Result:
    """`log_paths` are successive DEFAULT-BRANCH runs' logs, OLDEST-FIRST
    (one combined log per run). A guard that disables cache writes is
    allowed as a transient, surfaced refusal; `threshold` or more
    consecutive runs each showing it is the silent permanent freeze
    CACHE-026 forbids -- the guard is not self-healing (it never retires
    the superseded entries that keep it over its limit). A log with no
    disable signal breaks the streak."""

    per_log = [_guard_event(p) for p in log_paths]
    events = tuple(ev for ev in per_log if ev is not None)
    streak = _longest_streak(per_log)
    if threshold < 1 or len(streak) < threshold:
        return Cache026Result(events, ())
    finding = Finding(
        rule="CACHE-026",
        path=f"cache:log:{streak[0].source}",
        message=f"a cache guard disabled cache writes on {len(streak)} consecutive default-branch run(s) "
        f"(>= {threshold}): caches are frozen and every PR restores a stale generation "
        f"({', '.join(e.source for e in streak)}); first reason: {streak[0].reason}",
        fix="make the guard self-healing: when over its limit, retire superseded entries first "
        "(ci-lint cache janitor / preprune, oldest non-current lock first) and re-forecast, and only "
        "then refuse writes, with a failed or annotated cache-budget verdict -- never a permanent "
        "restore-only state (CACHE-026, zccache 2026-10-22..26)",
    )
    return Cache026Result(events, (finding,))


@dataclass(frozen=True)
class SaveEvidenceReport:
    skip_events: tuple[SaveSkipEvent, ...]
    unusable_events: tuple[UnusableRestoreEvent, ...]
    findings: tuple[Finding, ...]
    guard_events: tuple[GuardDisabledEvent, ...] = ()


def run_save_check(
    log_paths: list[Path],
    *,
    conclusion: str,
    run_url: str | None = None,
    threshold: int = DEFAULT_UNUSABLE_STREAK_THRESHOLD,
    guard_threshold: int = DEFAULT_GUARD_FREEZE_THRESHOLD,
) -> SaveEvidenceReport:
    r011 = check_cache_011(log_paths, conclusion=conclusion, run_url=run_url)
    r012 = check_cache_012(log_paths, threshold=threshold)
    r026 = check_cache_026(log_paths, threshold=guard_threshold)
    return SaveEvidenceReport(
        skip_events=r011.events,
        unusable_events=r012.events,
        findings=r011.findings + r012.findings + r026.findings,
        guard_events=r026.events,
    )


def to_json_dict(report: SaveEvidenceReport) -> dict[str, JsonValue]:
    return {
        "skip_events": [
            {"source": e.source, "layer": e.layer, "reason": e.reason} for e in report.skip_events
        ],
        "unusable_events": [
            {"source": e.source, "layer": e.layer, "archive_bytes": e.archive_bytes}
            for e in report.unusable_events
        ],
        "guard_disabled_events": [{"source": e.source, "reason": e.reason} for e in report.guard_events],
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: SaveEvidenceReport) -> str:
    lines = [
        f"ci-lint cache save-check: {len(report.skip_events)} skip event(s), "
        f"{len(report.unusable_events)} unusable-restore event(s), "
        f"{len(report.guard_events)} guard-disabled-writes event(s)"
    ]
    if not report.findings:
        lines.append("ci-lint cache save-check: no findings.")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint cache save-check: {len(report.findings)} violation(s)")
    return "\n".join(lines)
