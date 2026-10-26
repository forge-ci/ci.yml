"""`ci-lint suite check` -- ci_lint/runtime/suite.py (round-6C, TEST-001/002).

Uses `ci_lint/tests/fixtures/runtime/suite/`: one ci.toml with a required
`unit`/`smoke` and a non-required `integration` suite, a libtest-format
log per unit scenario (clean / one ignored test / zero tests executed,
the last with a merged-doctest "test result:" line summed in), and a
pytest JUnit XML per smoke scenario (clean / two tests skipped --
zackees/zccache#1760's `action_surface`/`test_cli.py` shape, where the CLI
was never actually on PATH).
"""

from __future__ import annotations

import unittest

from ci_lint.runtime.suite import SuiteCheckError, compute_suite_check, render_text
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]


class SuiteCheckRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FIXTURES / "runtime" / "suite"
        _loaded = load_ci_toml(self.repo)
        self.ci = _loaded.ci
        findings = _loaded.findings
        self.assertIsNotNone(self.ci, msg=str(findings))

    # -- RED: TEST-001, a skip inside a required suite -----------------

    def test_required_suite_ignored_test_is_test_001(self) -> None:
        report = compute_suite_check(self.ci, "unit", [self.repo / "unit-skip.cargo.log"], [])
        self.assertIn("TEST-001", rule_ids(report.findings))
        self.assertNotIn("TEST-002", rule_ids(report.findings))
        self.assertEqual(1, report.total_ignored)
        self.assertEqual(2, report.total_executed)

    def test_required_suite_pytest_skip_is_test_001(self) -> None:
        report = compute_suite_check(self.ci, "smoke", [], [self.repo / "smoke-skip.xml"])
        self.assertIn("TEST-001", rule_ids(report.findings))
        self.assertEqual(2, report.total_ignored)
        self.assertEqual(2, report.total_executed)

    # -- RED: TEST-002, zero executed tests in a required suite --------

    def test_required_suite_zero_executed_is_test_002(self) -> None:
        report = compute_suite_check(self.ci, "unit", [self.repo / "unit-empty.cargo.log"], [])
        self.assertIn("TEST-002", rule_ids(report.findings))
        self.assertEqual(0, report.total_executed)

    def test_required_suite_no_sources_is_test_002(self) -> None:
        report = compute_suite_check(self.ci, "unit", [], [])
        self.assertIn("TEST-002", rule_ids(report.findings))

    # -- GREEN: a clean required-suite run has no findings --------------

    def test_required_suite_clean_cargo_run_is_green(self) -> None:
        report = compute_suite_check(self.ci, "unit", [self.repo / "unit-ok.cargo.log"], [])
        self.assertEqual((), report.findings)
        # Two "test result:" lines (unit + doctest) are summed, proving the
        # merged-doctest case is handled.
        self.assertEqual(4, report.total_passed)
        self.assertEqual(4, report.total_executed)
        self.assertEqual(0, report.total_ignored)

    def test_required_suite_clean_pytest_run_is_green(self) -> None:
        report = compute_suite_check(self.ci, "smoke", [], [self.repo / "smoke-ok.xml"])
        self.assertEqual((), report.findings)
        self.assertEqual(4, report.total_passed)

    # -- Non-required suites: counts only, never a finding ---------------

    def test_non_required_suite_with_skip_is_counts_only(self) -> None:
        report = compute_suite_check(
            self.ci, "integration", [self.repo / "integration-skip.cargo.log"], []
        )
        self.assertEqual((), report.findings)
        self.assertEqual(1, report.total_ignored)

    def test_non_required_suite_with_no_sources_is_counts_only(self) -> None:
        report = compute_suite_check(self.ci, "integration", [], [])
        self.assertEqual((), report.findings)
        self.assertEqual(0, report.total_executed)

    # -- Multiple sources are summed into one total -----------------------

    def test_multiple_sources_are_summed(self) -> None:
        report = compute_suite_check(
            self.ci,
            "unit",
            [self.repo / "unit-ok.cargo.log", self.repo / "unit-skip.cargo.log"],
            [],
        )
        self.assertEqual(2, len(report.sources))
        self.assertEqual(6, report.total_passed)
        self.assertEqual(1, report.total_ignored)
        self.assertIn("TEST-001", rule_ids(report.findings))

    # -- Errors: exit-2 shaped, never silently "zero tests" ---------------

    def test_unknown_suite_raises(self) -> None:
        with self.assertRaises(SuiteCheckError):
            compute_suite_check(self.ci, "does-not-exist", [], [])

    def test_missing_cargo_log_raises(self) -> None:
        with self.assertRaises(SuiteCheckError):
            compute_suite_check(self.ci, "unit", [self.repo / "does-not-exist.log"], [])

    def test_cargo_log_without_a_test_result_line_raises(self) -> None:
        with self.assertRaises(SuiteCheckError):
            compute_suite_check(self.ci, "unit", [self.repo / "ci.toml"], [])

    def test_missing_junit_file_raises(self) -> None:
        with self.assertRaises(SuiteCheckError):
            compute_suite_check(self.ci, "smoke", [], [self.repo / "does-not-exist.xml"])

    def test_render_text_does_not_crash(self) -> None:
        report = compute_suite_check(self.ci, "unit", [self.repo / "unit-ok.cargo.log"], [])
        text = render_text(report)
        self.assertIn("unit", text)
        self.assertIn("TOTAL", text)


if __name__ == "__main__":
    unittest.main()
