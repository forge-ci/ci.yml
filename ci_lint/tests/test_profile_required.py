"""GEN-003 -- ci_lint/rules/profile_required.py (M2-21, ci.yml#44 part B).

A `[suites.<id>].required = true` suite must actually be invoked by some
workflow/composite-action step, and that step must not be disabled with a
literal `if: false`.
"""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.profile_required import check_group13
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


@requires_yaml_tooling
class Gen003Test(unittest.TestCase):
    def test_missing_required_suite_step_is_needs_review(self) -> None:
        repo = fixture("GEN-003", "red")
        ci = load_ci_toml(repo).ci
        assert ci is not None
        findings = check_group13(ci, repo)
        rules = [f.rule for f in findings]
        self.assertIn("GEN-003", rules)
        smoke_findings = [f for f in findings if "smoke" in f.message]
        self.assertTrue(smoke_findings, msg=findings)
        self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in smoke_findings), msg=findings)

    def test_required_suite_disabled_with_literal_if_false(self) -> None:
        repo = fixture("GEN-003", "red-if-false")
        ci = load_ci_toml(repo).ci
        assert ci is not None
        findings = check_group13(ci, repo)
        smoke_findings = [f for f in findings if "smoke" in f.message]
        self.assertTrue(smoke_findings, msg=findings)
        self.assertTrue(all(f.status == Status.VIOLATION for f in smoke_findings), msg=findings)
        self.assertFalse(any("unit" in f.message for f in findings), msg=findings)

    def test_green_all_required_suites_invoked(self) -> None:
        repo = fixture("GEN-003", "green")
        ci = load_ci_toml(repo).ci
        assert ci is not None
        findings = check_group13(ci, repo)
        self.assertEqual([], findings, msg=findings)


if __name__ == "__main__":
    unittest.main()
