"""`ci-lint suite check`: TEST-001/002 from real test-run evidence.

Round-6C brief, item 2. Neither rule can be decided statically: TEST-001
("a skip in a required suite is a failure") and TEST-002 ("zero executed
tests in a required suite") both need to know what a real test run
actually did, not what the harness declares. This module never runs a
test itself -- it reads a libtest-format log captured from
`cargo test`/`soldr cargo test`/nextest's libtest-compatible summary line,
and/or a pytest JUnit XML report, and checks the counts against the suite
ci.toml declares.

zackees/zccache#1760 is the motivating case: `action_surface` logged
"no template-cli binary found ... Skipping" and `test_cli.py` silently
skipped 2 tests because the CLI was never actually on PATH, and CI stayed
green throughout. TEST-001 turns that skip into a failure when the suite
is required; TEST-002 catches the same gap in its most extreme form (the
template's `running 0 tests` baseline, round-0 evidence in issue #6).

AGENTS.md's typed-boundary rule applies: the JUnit XML is read with
stdlib `xml.etree.ElementTree` and converted straight into frozen
dataclasses; the libtest log is parsed with a compiled regex into the
same shape. Nothing downstream of `_parse_cargo_test_log`/
`_parse_pytest_junit` sees a raw dict or XML element.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding
from ci_lint.schema import CiToml

# "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered
# out; finished in 0.00s" -- the classic libtest summary line that
# `cargo test`, `soldr cargo test` and `cargo nextest`'s libtest-compatible
# archive runner all emit once per test binary. A single log can hold
# several of these (one per binary); a merged-doctest binary
# (`RUST-011`'s "doctests not merged" case, once fixed) just contributes
# one more line, so every match in the file is summed rather than only
# the first or last.
LIBTEST_RESULT_RE = re.compile(
    r"test result:\s*\w+\.\s*"
    r"(?P<passed>\d+)\s+passed;\s*"
    r"(?P<failed>\d+)\s+failed;\s*"
    r"(?P<ignored>\d+)\s+ignored;"
)


class SuiteCheckError(Exception):
    """An unknown `--suite` id, or a log/JUnit file that could not be read
    or parsed as the format it claims to be -- exit 2. Never silently
    treated as "zero tests ran": that would make a tooling failure look
    identical to the TEST-002 violation it exists to catch."""


@dataclass(frozen=True)
class SuiteSourceCounts:
    """One `--cargo-test-log`/`--pytest-junit` file's contribution."""

    source: str
    kind: str  # "cargo-test-log" | "pytest-junit"
    passed: int
    failed: int
    ignored: int

    @property
    def executed(self) -> int:
        return self.passed + self.failed


@dataclass(frozen=True)
class SuiteCheckReport:
    suite_id: str
    required: bool
    sources: tuple[SuiteSourceCounts, ...]
    findings: tuple[Finding, ...]

    @property
    def total_passed(self) -> int:
        return sum(s.passed for s in self.sources)

    @property
    def total_failed(self) -> int:
        return sum(s.failed for s in self.sources)

    @property
    def total_ignored(self) -> int:
        return sum(s.ignored for s in self.sources)

    @property
    def total_executed(self) -> int:
        return sum(s.executed for s in self.sources)


def _parse_cargo_test_log(path: Path) -> SuiteSourceCounts:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SuiteCheckError(f"cannot read --cargo-test-log {path}: {exc}") from exc
    matches = list(LIBTEST_RESULT_RE.finditer(text))
    if not matches:
        raise SuiteCheckError(
            f"{path}: no 'test result: <outcome>. N passed; M failed; K ignored;' line found -- "
            "is this a raw stdout capture of 'cargo test'/'soldr cargo test'/a nextest libtest "
            "summary, not a filtered or truncated log?"
        )
    passed = sum(int(m.group("passed")) for m in matches)
    failed = sum(int(m.group("failed")) for m in matches)
    ignored = sum(int(m.group("ignored")) for m in matches)
    return SuiteSourceCounts(
        source=str(path), kind="cargo-test-log", passed=passed, failed=failed, ignored=ignored
    )


@dataclass(frozen=True)
class _JunitTotals:
    tests: int
    failures: int
    errors: int
    skipped: int


def _sum_junit_suite(elem: ET.Element) -> _JunitTotals:
    def _int_attr(name: str) -> int:
        raw = elem.get(name)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    return _JunitTotals(_int_attr("tests"), _int_attr("failures"), _int_attr("errors"), _int_attr("skipped"))


def _parse_pytest_junit(path: Path) -> SuiteSourceCounts:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise SuiteCheckError(f"cannot parse --pytest-junit {path}: {exc}") from exc

    if root.tag == "testsuites":
        suite_elems = list(root.findall("testsuite"))
        if not suite_elems:
            raise SuiteCheckError(f"{path}: <testsuites> root has no <testsuite> children")
    elif root.tag == "testsuite":
        suite_elems = [root]
    else:
        raise SuiteCheckError(
            f"{path}: root element is <{root.tag}>, not the <testsuite>/<testsuites> pytest "
            "JUnit XML produces"
        )

    tests = failures = errors = skipped = 0
    for elem in suite_elems:
        totals = _sum_junit_suite(elem)
        tests += totals.tests
        failures += totals.failures
        errors += totals.errors
        skipped += totals.skipped

    failed = failures + errors
    passed = tests - failed - skipped
    if passed < 0:
        raise SuiteCheckError(
            f"{path}: tests={tests} is less than failures({failures}) + errors({errors}) + "
            f"skipped({skipped}) -- malformed JUnit report"
        )
    return SuiteSourceCounts(
        source=str(path), kind="pytest-junit", passed=passed, failed=failed, ignored=skipped
    )


def compute_suite_check(
    ci: CiToml,
    suite_id: str,
    cargo_test_logs: list[Path],
    pytest_junit_files: list[Path],
) -> SuiteCheckReport:
    suite = ci.suites.get(suite_id)
    if suite is None:
        raise SuiteCheckError(
            f"ci.toml has no [suites.{suite_id}]; declared suites: {sorted(ci.suites)}"
        )

    sources: list[SuiteSourceCounts] = [_parse_cargo_test_log(p) for p in cargo_test_logs]
    sources.extend(_parse_pytest_junit(p) for p in pytest_junit_files)

    findings: list[Finding] = []
    total_ignored = sum(s.ignored for s in sources)
    total_executed = sum(s.executed for s in sources)

    if suite.required and total_ignored > 0:
        detail = ", ".join(f"{s.source} ({s.ignored})" for s in sources if s.ignored > 0)
        findings.append(
            Finding(
                rule="TEST-001",
                path="ci.toml",
                message=f"suite '{suite_id}' is [suites.{suite_id}].required = true but "
                f"{total_ignored} test(s) were ignored/skipped: {detail}",
                fix=f"[suites.{suite_id}].required = true in ci.toml means every test it declares "
                "must actually run -- remove the #[ignore]/@pytest.mark.skip, fix what it is "
                "skipping for (e.g. a binary missing from PATH), or move this test to a "
                "non-required suite",
            )
        )
    if suite.required and total_executed == 0:
        findings.append(
            Finding(
                rule="TEST-002",
                path="ci.toml",
                message=f"suite '{suite_id}' is [suites.{suite_id}].required = true but executed 0 "
                f"tests across {len(sources)} source(s)",
                fix=f"[suites.{suite_id}].required = true in ci.toml; add at least one test that "
                "actually runs and pass its real --cargo-test-log/--pytest-junit output, or make "
                "the suite optional if it genuinely has no tests yet",
            )
        )

    return SuiteCheckReport(
        suite_id=suite_id,
        required=suite.required,
        sources=tuple(sources),
        findings=tuple(findings),
    )


def to_json_dict(report: SuiteCheckReport) -> dict[str, JsonValue]:
    return {
        "suite_id": report.suite_id,
        "required": report.required,
        "sources": [
            {
                "source": s.source,
                "kind": s.kind,
                "passed": s.passed,
                "failed": s.failed,
                "ignored": s.ignored,
                "executed": s.executed,
            }
            for s in report.sources
        ],
        "total_passed": report.total_passed,
        "total_failed": report.total_failed,
        "total_ignored": report.total_ignored,
        "total_executed": report.total_executed,
        "findings": [
            {
                "rule": f.rule,
                "status": f.status.value,
                "path": f.path,
                "line": f.line,
                "message": f.message,
                "fix": f.fix,
            }
            for f in report.findings
        ],
    }


def render_text(report: SuiteCheckReport) -> str:
    lines = [f"ci-lint suite check: suite '{report.suite_id}' (required={report.required})"]
    lines.append(f"{'source':<50} {'kind':<14} {'passed':>7} {'failed':>7} {'ignored':>8}")
    for s in report.sources:
        lines.append(f"{s.source:<50} {s.kind:<14} {s.passed:>7} {s.failed:>7} {s.ignored:>8}")
    lines.append(
        f"{'TOTAL':<50} {'':<14} {report.total_passed:>7} {report.total_failed:>7} "
        f"{report.total_ignored:>8}  (executed={report.total_executed})"
    )
    if not report.required:
        lines.append("(not required -- counts only, never a finding)")
    lines.append("")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint suite check: {len(report.findings)} violation(s)")
    return "\n".join(lines)
