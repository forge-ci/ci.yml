"""`ci-lint cache rework`: CACHE-035 from real job-log evidence (#148 p1).

Two of the three rework signals the running-process retrospective
prioritized (#148), implemented from captured logs rather than heuristics
-- the input convention matches `cache save-check` (RUST-003/TEST-001
precedent): `gh run view --log`, or any captured stdout of the same; no
network access. The third signal (serial-execution ratio) needs step
timing the log body does not carry reliably and stays unimplemented.

Signal A -- rebuild after an exact hit: the job's restore evidence reports
at least one layer `HIT` (or an `N/M exact-hit` summary), yet cargo still
printed `Compiling <crate> <version>` lines. The store existed and was
restored, and rustc ran anyway: that is the kernal-api lesson-1 class
("a cache that claimed to work" -- worth ~6 min/PR there).

Signal B -- the same crate compiled in more than one step: cargo's
`Compiling <crate> <ver>` under two distinct step names (across the
logs given). One compile per crate per job; a second step paying it
again is rework (an ordering/`target/`-sharing defect like RUST-016's
family).

Evidence formats these patterns were read from (2026-10-25): bosn run
40172648545 job `Rust workspace` (restore table `build-cache HIT …
330.3MB`, `2/3 exact-hit  2/3 any-hit  total restore: 12.6s`, per-unit
`soldr[cache] <unit> [HIT!]` with ANSI; TWO tab fields with the ISO
timestamp glued to the content) and talos-hq/clud run 40188114016.

Status: a recompile-after-hit can have causes outside the log's view
(feature/flag drift, an unkeyed input), so both signals report
`needs_review` -- evidence for the diagnosis, never a silent pass and
never an auto-delete. Pair with `ci-lint cache save-check` (CACHE-011/
012: the writer side) and, for hit-rate *reporting* in step summaries,
issue #411's separate proposal.

Records here are typed dataclasses (PY-002): no tuple/dict records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status

RULE = "CACHE-035"


class ReworkError(ValueError):
    """A `--log` file could not be read."""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# cargo: `Compiling unicode_ident v1.1.12` (crate names never contain
# spaces; the version is whatever cargo prints).
_COMPILING_RE = re.compile(r"\bCompiling (?P<crate>[A-Za-z0-9_-]+) v(?P<version>[0-9][0-9A-Za-z.+-]*)")
# setup-soldr/zccache restore table row: `build-cache           HIT       <key>  330.3MB`
# (status column is `HIT`/`MISS (no match)`/..., whitespace-aligned).
_LAYER_HIT_RE = re.compile(r"^(?P<layer>[A-Za-z][\w.-]*)\s+HIT\b")
_EXACT_HIT_SUMMARY_RE = re.compile(r"\b\d+/\d+ exact-hit\b")
# `gh run view --log` reality (verified on bosn run 40172648545): TWO tab
# fields, then `<ISO-8601-Z> <content>` glued -- not the four-field shape
# older exporters used.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")


@dataclass(frozen=True)
class CompileEvent:
    """One `Compiling` line, tied to the step that paid it."""

    step: str
    crate: str
    version: str


@dataclass(frozen=True)
class StepLine:
    """One log line with its (possibly empty) step name."""

    step: str
    content: str


@dataclass(frozen=True)
class LayerHit:
    """One restore-table `HIT` row: layer name and how often it hit."""

    layer: str
    count: int


@dataclass(frozen=True)
class ParsedLog:
    """What `parse_log` found in one job log."""

    compiles: tuple[CompileEvent, ...]
    warm: bool
    layers: tuple[LayerHit, ...]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _lines_with_steps(log: str) -> tuple[StepLine, ...]:
    """(step, content) per line: `gh --log` style (`job\\tstep\\t<ts> <line>`,
    or the four-field variant some exporters emit) or bare stdout (step stays
    "" -- the caller passes one job's output)."""

    out: list[StepLine] = []
    for raw in log.splitlines():
        parts = raw.split("\t")
        if len(parts) >= 3:
            rest = parts[3] if len(parts) >= 4 else parts[2]
            content = _TIMESTAMP_RE.sub("", _strip_ansi(rest), count=1)
            out.append(StepLine(parts[1].strip(), content))
        else:
            out.append(StepLine("", _strip_ansi(raw)))
    return tuple(out)


def parse_log(log: str) -> ParsedLog:
    """Compile events, whether a warm restore was seen, and per-layer HIT counts."""

    compiles: list[CompileEvent] = []
    warm = False
    layer_counts: dict[str, int] = {}
    for line in _lines_with_steps(log):
        if _EXACT_HIT_SUMMARY_RE.search(line.content):
            warm = True
        hit = _LAYER_HIT_RE.match(line.content.strip())
        if hit is not None:
            warm = True
            layer_counts[hit.group("layer")] = layer_counts.get(hit.group("layer"), 0) + 1
        for m in _COMPILING_RE.finditer(line.content):
            compiles.append(CompileEvent(line.step, m.group("crate"), m.group("version")))
    layers = tuple(LayerHit(name, count) for name, count in sorted(layer_counts.items()))
    return ParsedLog(tuple(compiles), warm, layers)


def check_rework(logs: list[str]) -> list[Finding]:
    """CACHE-035: rebuild-after-hit (A) and cross-step recompiles (B)."""

    if not logs:
        return []
    findings: list[Finding] = []
    all_events: list[CompileEvent] = []
    warm_total = False
    for index, text in enumerate(logs):
        parsed = parse_log(text)
        all_events.extend(parsed.compiles)
        warm_total = warm_total or parsed.warm
        if parsed.warm and parsed.compiles:
            findings.append(_rebuild_after_hit(index, parsed))
    findings.extend(_cross_step_recompiles(all_events))
    return findings


def _rebuild_after_hit(index: int, parsed: ParsedLog) -> Finding:
    crates = sorted({f"{event.crate} v{event.version}" for event in parsed.compiles})
    sample = ", ".join(crates[:5]) + (" …" if len(crates) > 5 else "")
    layers_hit = ", ".join(f"{item.layer} HIT×{item.count}" for item in parsed.layers)
    return Finding(
        rule=RULE,
        path=f"log[{index}]",
        status=Status.NEEDS_REVIEW,
        message=f"warm restore still rebuilt {len(crates)} crate(s): [{layers_hit}] yet cargo "
        f"printed Compiling for {sample} -- the store existed and rustc ran anyway "
        "(kernal-api's 'cache that claimed to work', zackees/ci.yml#148)",
        fix="compare the compiled crate's inputs against the cache key's scope (feature "
        "flags, target, toolchain digest, unkeyed files): either the key misses an input "
        "the build reads, or the HIT layer does not cover this unit. Pair with "
        "`ci-lint cache save-check` for the writer side",
    )


def _cross_step_recompiles(events: list[CompileEvent]) -> list[Finding]:
    steps_by_crate: dict[str, list[str]] = {}
    for event in events:
        steps_by_crate.setdefault(f"{event.crate}@{event.version}", []).append(event.step)
    findings: list[Finding] = []
    for key in sorted(steps_by_crate):
        distinct = sorted({step for step in steps_by_crate[key] if step})
        if len(distinct) < 2:
            continue
        crate, _, version = key.partition("@")
        findings.append(
            Finding(
                rule=RULE,
                path="logs",
                status=Status.NEEDS_REVIEW,
                message=f"`{crate} v{version}` compiled in {len(distinct)} distinct steps "
                f"({', '.join(distinct)}) -- one compile per crate per job; a second step "
                "paying it again is rework (zackees/ci.yml#148)",
                fix="share one `target/` between the steps or order them so the second "
                "consumes the first's outputs (RUST-016's family: two writers to one "
                "immutable key, or a rebuild of a tree the other step already built)",
            )
        )
    return findings


@dataclass(frozen=True)
class ReworkReport:
    """`cache rework` output: findings plus how much compiled and how warm."""

    findings: tuple[Finding, ...]
    compile_events: int
    warm: bool
    layers_hit: tuple[LayerHit, ...]

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "findings": [
                {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
                for f in self.findings
            ],
            "compile_events": self.compile_events,
            "warm_restore": self.warm,
            "layers_hit": {item.layer: item.count for item in self.layers_hit},
        }


def run_rework(log_paths: list[str]) -> ReworkReport:
    """Read the given job-log files (pure filesystem, no token -- the caller
    already has them, per the save-check/RUST-003 `--log` convention) and
    return the CACHE-035 verdict."""

    texts: list[str] = []
    for raw in log_paths:
        try:
            texts.append(Path(raw).read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            raise ReworkError(f"{raw}: {error}") from error
    findings = tuple(check_rework(texts))
    events = 0
    warm = False
    layer_counts: dict[str, int] = {}
    for text in texts:
        parsed = parse_log(text)
        events += len(parsed.compiles)
        warm = warm or parsed.warm
        for item in parsed.layers:
            layer_counts[item.layer] = layer_counts.get(item.layer, 0) + item.count
    layers = tuple(LayerHit(name, count) for name, count in sorted(layer_counts.items()))
    return ReworkReport(findings, events, warm, layers)


def render_rework_text(report: ReworkReport) -> str:
    if not report.findings:
        layers = {item.layer: item.count for item in report.layers_hit}
        return (
            f"ci-lint cache rework: no rework signals (compile events={report.compile_events}, "
            f"warm restore={report.warm}, layers={layers or '-'})"
        )
    lines = [f"== CACHE-035 ({len(report.findings)}) =="]
    for f in report.findings:
        lines.append(f"[{f.status.value}] {f.path}: {f.message}")
        lines.append(f"    fix: {f.fix}")
    return "\n".join(lines)