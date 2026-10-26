"""`ci-lint dylint coverage`: RUST-003 from real Dylint job evidence.

Round-6E brief, item 2 (zackees/ci.yml#1's acceptance criteria: "a
cross-target job missing the target `rust-std` component" / "a Linux-only
job that omits intended platform lint coverage" must be IDENTIFIED, not
inferred from the runner OS). Neither can be decided statically -- this
module never runs Dylint itself. It reads evidence from one or more
`--log` files, each auto-detected as one of two real shapes:

  - `ci/dylint.py`'s own `--results-out` file: a JSON array of
    `DylintPassResult` records (`{shape, targets, command, seconds,
    returncode}` -- template-python-rust-cmd's own struct, see its
    `ci/dylint.py`). The reliable source: `targets` already names every
    triple (host included) one invocation covered, and `returncode` says
    outright whether that invocation succeeded. Parsed straight into
    `DylintPassResultRecord` frozen dataclasses at the JSON boundary
    (AGENTS.md's typed-boundary rule -- no raw dict survives past
    `_load_results_json`).
  - A raw GitHub Actions job log (`gh run view --job <id> --log`, or any
    captured stdout of the same): matched with compiled regexes for the
    `+ soldr dylint ...`/`+ soldr cargo dylint ...` invocation trace line
    (the shell's own `set -x`-style echo, from `ci/dylint.py`'s `_run`),
    rustc's `Checking with toolchain \\`...\\`` banner (names the HOST
    triple as a suffix -- there is no `--target` flag for the host's own
    native check), a `Finished` profile-completion line, and an
    `error[E0463]: can't find crate for \\`core\\`` + its `the \\`<target>\\`
    target may not be installed` note.

Coverage: every target in `ci_lint.plan.dylint_target_platform_ids(ci)`
(`ci.toml`'s declared `[platforms]`, when `[lint.dylint].targets =
"all-platforms"`) must appear as a COMPLETED check pass in the combined
evidence from every `--log` given. A multi-target invocation covering
several `--target` flags (or one `DylintPassResultRecord.targets` entry
naming several triples) counts for every triple it names -- issue #6's D6
finding that one Linux job checks every declared platform from a single
pass, reproduced live in zackees/template-python-rust-cmd.

An `E0463: can't find crate for \\`core\\`` failure is reported as its own,
more specific RUST-003 finding ("the target's rust-std was never
provisioned") rather than only the generic "not covered" one -- issue #1's
own fbuild evidence (run 36347775473) names this exact error.

zackees/setup-soldr#538/#540 (RUST-004, `ci_lint.cache.audit`) is the
"succeeded but didn't save its cache" defect; this module is the other half
issue #1 asks for -- "did the Dylint job actually cover the platform it
claims to," independent of caching.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding
from ci_lint.plan import dylint_target_platform_ids
from ci_lint.schema import CiToml

# A real ANSI escape (ESC 0x1b + '[' + params + a letter), OR the literal
# two-character '^[' rendering `gh run view --job <id> --log` actually
# emits in this environment (confirmed against a real fetch, round-6E --
# neither the real ESC byte nor a caret-bracket digraph survives into
# plain text otherwise, so stripping only one shape silently left the
# other's color codes sitting between a marker word and the text after
# it, e.g. "Finished^[[0m `dev`" never matching "Finished `dev`").
_ANSI_RE = re.compile(r"(?:\x1b\[|\^\[\[)[0-9;]*[A-Za-z]")
_INVOCATION_LINE_RE = re.compile(
    r"^.*\bsoldr (?:dylint|cargo dylint)\b.*--all-targets.*$", re.MULTILINE
)
_TARGET_FLAG_RE = re.compile(r"--target\s+(\S+)")
_TOOLCHAIN_RE = re.compile(r"Checking with toolchain `([^`]+)`")
_FINISHED_RE = re.compile(r"\bFinished\b `[^`]+` profile")
_E0463_RE = re.compile(r"error\[E0463\]: can't find crate for `core`")
_E0463_TARGET_RE = re.compile(r"the `([^`]+)` target may not be installed")


class DylintCoverageError(Exception):
    """An unreadable `--log` file -- exit 2, never silently treated as "no
    evidence" (matching `ci_lint.runtime.suite.SuiteCheckError`'s
    convention: a tooling failure must never look identical to the
    violation it exists to catch)."""


@dataclass(frozen=True)
class DylintPassResultRecord:
    """One `ci/dylint.py` `DylintPassResult` JSON entry, typed at the
    boundary (AGENTS.md's benchmark/record-dataclass rule)."""

    shape: str
    targets: tuple[str, ...]
    returncode: int


@dataclass(frozen=True)
class DylintLogEvidence:
    """What one `--log` source (either shape) contributed."""

    source: str
    kind: str  # "results-json" | "raw-log"
    completed_targets: tuple[str, ...]  # triples proven covered by a returncode==0 / finished pass
    failed_targets: tuple[str, ...]  # triples named by an attempted-but-failed pass
    toolchain_strings: tuple[str, ...]  # raw-log only: "Checking with toolchain `X`" -- host detection
    missing_std_targets: tuple[str, ...]  # E0463 "target may not be installed" triples


@dataclass(frozen=True)
class RequiredTarget:
    """A `[platforms]` entry the plan requires a Dylint pass for."""

    platform_id: str
    target: str


@dataclass(frozen=True)
class DylintCoverageReport:
    required: tuple[RequiredTarget, ...]  # the targets the plan requires
    sources: tuple[DylintLogEvidence, ...]
    covered_targets: frozenset[str]
    findings: tuple[Finding, ...]


def _load_results_json(path: Path, text: str) -> list[DylintPassResultRecord] | None:
    """`None` when `text` isn't this shape at all (falls back to raw-log
    parsing); raises `DylintCoverageError` when it LOOKS like this shape
    (starts with `[`) but is malformed -- never silently degrades a broken
    `--results-out` file to "zero evidence"."""

    if not text.lstrip().startswith("["):
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DylintCoverageError(f"{path}: looks like --results-out JSON but failed to parse: {exc}") from exc
    if not isinstance(raw, list):
        raise DylintCoverageError(f"{path}: --results-out JSON root must be an array of DylintPassResult")
    records: list[DylintPassResultRecord] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DylintCoverageError(f"{path}: entry {i} is not an object")
        targets = item.get("targets")
        rc = item.get("returncode")
        shape = item.get("shape")
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise DylintCoverageError(f"{path}: entry {i} has no string[] 'targets'")
        if not isinstance(rc, int):
            raise DylintCoverageError(f"{path}: entry {i} has no integer 'returncode'")
        records.append(
            DylintPassResultRecord(
                shape=shape if isinstance(shape, str) else "", targets=tuple(targets), returncode=rc
            )
        )
    return records


def _evidence_from_results(path: Path, records: list[DylintPassResultRecord]) -> DylintLogEvidence:
    completed: set[str] = set()
    failed: set[str] = set()
    for r in records:
        if r.returncode == 0:
            completed.update(r.targets)
        else:
            failed.update(r.targets)
    return DylintLogEvidence(
        source=str(path),
        kind="results-json",
        completed_targets=tuple(sorted(completed)),
        failed_targets=tuple(sorted(failed - completed)),
        toolchain_strings=(),
        missing_std_targets=(),
    )


def _evidence_from_raw_log(path: Path, text: str) -> DylintLogEvidence:
    clean = _ANSI_RE.sub("", text)

    invocation_targets: set[str] = set()
    saw_invocation = False
    for match in _INVOCATION_LINE_RE.finditer(clean):
        saw_invocation = True
        invocation_targets.update(_TARGET_FLAG_RE.findall(match.group(0)))

    toolchain_matches = list(_TOOLCHAIN_RE.finditer(clean))
    toolchains = tuple(sorted({m.group(1) for m in toolchain_matches}))
    # A "Finished ... profile" line BEFORE "Checking with toolchain" is a
    # lint-LIBRARY build (dylints/*'s own release-profile compile), not the
    # workspace check pass -- only a completion marker printed AFTER the
    # toolchain banner proves the actual target check(s) finished.
    after_toolchain = clean[toolchain_matches[-1].end() :] if toolchain_matches else ""
    finished = bool(_FINISHED_RE.search(after_toolchain))
    missing_std = tuple(sorted(set(_E0463_TARGET_RE.findall(clean)))) if _E0463_RE.search(clean) else ()

    # Only trust the invocation's --target list as "completed" when the
    # pass actually finished cleanly with no E0463 -- an invocation that
    # started but never printed a completion marker (aborted build, one
    # target's compile error taking the whole cargo invocation down with
    # it) proves nothing about ANY of the targets it named. Unclear ->
    # unknown (AGENTS.md), never silently treated as covered.
    completed = invocation_targets if (saw_invocation and finished and not missing_std) else set()
    failed = invocation_targets if (saw_invocation and not completed) else set()

    return DylintLogEvidence(
        source=str(path),
        kind="raw-log",
        completed_targets=tuple(sorted(completed)),
        failed_targets=tuple(sorted(failed)),
        toolchain_strings=toolchains,
        missing_std_targets=missing_std,
    )


def _parse_log(path: Path) -> DylintLogEvidence:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DylintCoverageError(f"cannot read --log {path}: {exc}") from exc
    records = _load_results_json(path, text)
    if records is not None:
        return _evidence_from_results(path, records)
    return _evidence_from_raw_log(path, text)


def compute_dylint_coverage(ci: CiToml, log_paths: list[Path]) -> DylintCoverageReport:
    required_ids = dylint_target_platform_ids(ci)
    required = tuple(RequiredTarget(pid, ci.platforms[pid].target) for pid in required_ids)

    sources = tuple(_parse_log(p) for p in log_paths)

    covered: set[str] = set()
    for ev in sources:
        covered.update(ev.completed_targets)
        for toolchain in ev.toolchain_strings:
            for target in (r.target for r in required):
                if toolchain.endswith(target):
                    covered.add(target)

    missing_std_targets: dict[str, list[str]] = {}
    for ev in sources:
        for target in ev.missing_std_targets:
            missing_std_targets.setdefault(target, []).append(ev.source)

    findings: list[Finding] = []
    evidence_desc = ", ".join(s.source for s in sources) if sources else "(no --log given)"

    for target, log_sources in sorted(missing_std_targets.items()):
        findings.append(
            Finding(
                rule="RUST-003",
                path=", ".join(log_sources),
                message=f"Dylint check for target {target!r} failed with E0463 (can't find crate for "
                "`core`) -- its rust-std was never provisioned (setup-soldr#538/#540-class gap; "
                "issue #1's fbuild run 36347775473 is the same failure mode)",
                fix=f"provision the prebuilt target std before checking: 'soldr dylint prepare --target "
                f"{target}' must run (and succeed) before the check pass that names --target {target} "
                "-- confirm the prepare step actually ran for this target and that its rc was 0",
            )
        )

    for pid, target in ((r.platform_id, r.target) for r in required):
        if target in covered or target in missing_std_targets:
            continue
        findings.append(
            Finding(
                rule="RUST-003",
                path="ci.toml",
                message=f"declared platform '{pid}' (target {target!r}) has no completed Dylint check "
                f"pass in the given evidence ({evidence_desc})",
                fix=f"add --target {target} to the Linux dylint job's 'soldr dylint --all -- "
                "--workspace --all-targets [--target T ...]' invocation (or, for the host platform, "
                "confirm the check pass reached 'Checking with toolchain' and a 'Finished ... profile' "
                f"line) so '{pid}' is actually covered, not just declared in ci.toml [platforms]",
            )
        )

    return DylintCoverageReport(
        required=required, sources=sources, covered_targets=frozenset(covered), findings=tuple(findings)
    )


def to_json_dict(report: DylintCoverageReport) -> dict[str, JsonValue]:
    return {
        "required": [{"platform_id": r.platform_id, "target": r.target} for r in report.required],
        "sources": [
            {
                "source": s.source,
                "kind": s.kind,
                "completed_targets": list(s.completed_targets),
                "failed_targets": list(s.failed_targets),
                "missing_std_targets": list(s.missing_std_targets),
            }
            for s in report.sources
        ],
        "covered_targets": sorted(report.covered_targets),
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: DylintCoverageReport) -> str:
    lines = [f"ci-lint dylint coverage: {len(report.required)} declared target(s), " f"{len(report.sources)} log source(s)"]
    lines.append(f"{'platform':<16} {'target':<26} {'covered':>7}")
    for pid, target in ((r.platform_id, r.target) for r in report.required):
        lines.append(f"{pid:<16} {target:<26} {'yes' if target in report.covered_targets else 'no':>7}")
    lines.append("")
    if not report.findings:
        lines.append("ci-lint dylint coverage: no findings.")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint dylint coverage: {len(report.findings)} violation(s)")
    return "\n".join(lines)
