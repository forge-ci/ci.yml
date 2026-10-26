"""GEN-019 -- ci_lint/rules/lint_job_tests.py (zackees/ci.yml#111)."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.lint_job_tests import check_gen_019, is_lint_job
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class LintJobClassifierTest(unittest.TestCase):
    def test_lint_jobs(self) -> None:
        for job_id, name in (
            ("lint", None),
            ("x", "Lint"),
            ("clippy", None),
            ("fast-lint", None),
            ("quick_lint", None),
            ("ci", "Rust lint (stable)"),
        ):
            self.assertTrue(is_lint_job(job_id, name), (job_id, name))

    def test_not_lint_jobs(self) -> None:
        for job_id, name in (("dylint", None), ("fast", None), ("unit-test", "Tests"), ("linter-x", None)):
            self.assertFalse(is_lint_job(job_id, name), (job_id, name))


@requires_yaml_tooling
class Gen019Test(unittest.TestCase):
    def test_green_is_clean(self) -> None:
        self.assertEqual([], check_gen_019(fixture("GEN-019", "green")))

    def test_green_same_line_allow_comment(self) -> None:
        self.assertEqual([], check_gen_019(fixture("GEN-019", "green-allow")))

    def test_red_run_lines(self) -> None:
        findings = check_gen_019(fixture("GEN-019", "red"))
        self.assertEqual(2, len(findings))
        self.assertTrue(all(f.rule == "GEN-019" and f.status == Status.VIOLATION for f in findings))
        text = " ".join(f.message for f in findings)
        self.assertIn("cargo nextest run", text)
        self.assertIn("pytest", text)
        self.assertIn("'preflight'", text)

    def test_red_one_level_into_ci_script(self) -> None:
        findings = check_gen_019(fixture("GEN-019", "red-script"))
        self.assertEqual(1, len(findings))
        f = findings[0]
        self.assertEqual(("GEN-019", "ci/lint.py", 4, Status.VIOLATION), (f.rule, f.path, f.line, f.status))


if __name__ == "__main__":
    unittest.main()
