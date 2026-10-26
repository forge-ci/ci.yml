"""Section F self-check: one GREEN fixture repo that passes every group.

`_e2e/green/` is a tiny but complete rust-pypi-app repo: it satisfies every
rule in groups 1-9, including the draft ci.toml's own `[[exceptions]]`
pattern (a tracked `ci.sh` that GEN-005 would otherwise flag, approved via
a non-expired exception).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ci_lint.finding import Status
from ci_lint.precheck import LOCAL_SKIPPED_CHECKS, has_violations, render_text, run_precheck
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class EndToEndGreenFixtureTest(unittest.TestCase):
    def test_precheck_is_fast_on_a_small_repo(self) -> None:
        result = run_precheck(fixture("_e2e", "green"), title="[ci-windows] x")
        self.assertLess(result.elapsed_seconds, 5.0)

    @requires_yaml_tooling
    def test_e2e_green_fixture_has_no_violations(self) -> None:
        result = run_precheck(fixture("_e2e", "green"), title="[ci-windows] x")
        violations = [f for f in result.findings if f.status == Status.VIOLATION]
        self.assertEqual([], violations, msg=render_text(result))
        self.assertFalse(has_violations(result))

    @requires_yaml_tooling
    def test_e2e_green_fixture_reports_the_ci_sh_exception(self) -> None:
        result = run_precheck(fixture("_e2e", "green"), title="[ci-windows] x")
        exceptions = [f for f in result.findings if f.status == Status.APPROVED_EXCEPTION]
        self.assertTrue(any(f.rule == "GEN-005" and f.path == "ci.sh" for f in exceptions))

    @requires_yaml_tooling
    def test_e2e_green_fixture_prints_cache_arithmetic(self) -> None:
        result = run_precheck(fixture("_e2e", "green"), title="[ci-windows] x")
        self.assertIsNotNone(result.cache_arithmetic)
        assert result.cache_arithmetic is not None
        self.assertIn("worst case", result.cache_arithmetic)


class LocalModeTest(unittest.TestCase):
    """Round-2A brief, part 2e: `--local` (or env ACT=true) must skip
    anything needing the GitHub API and say so explicitly -- never report
    it as passed."""

    @requires_yaml_tooling
    def test_default_run_has_no_local_skip_notices(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACT", None)
            result = run_precheck(fixture("_e2e", "green"), title="x")
        self.assertFalse(result.local)
        skip_rules = {c.rule for c in LOCAL_SKIPPED_CHECKS}
        self.assertEqual(set(), {f.rule for f in result.findings} & skip_rules)

    @requires_yaml_tooling
    def test_local_flag_reports_every_skipped_check_as_needs_review(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACT", None)
            result = run_precheck(fixture("_e2e", "green"), title="x", local=True)
        self.assertTrue(result.local)
        by_rule = {f.rule: f for f in result.findings}
        for rule in (c.rule for c in LOCAL_SKIPPED_CHECKS):
            self.assertIn(rule, by_rule, msg=render_text(result))
            f = by_rule[rule]
            self.assertEqual(Status.NEEDS_REVIEW, f.status)
            self.assertIn("skipped (local)", f.message)
        # never reported as passed, and never fails the run by itself:
        self.assertFalse(has_violations(result))

    @requires_yaml_tooling
    def test_act_true_env_triggers_local_mode_without_the_flag(self) -> None:
        with mock.patch.dict(os.environ, {"ACT": "true"}):
            result = run_precheck(fixture("_e2e", "green"), title="x", local=False)
        self.assertTrue(result.local)
        self.assertTrue(any(f.rule == "CACHE-005" for f in result.findings))


if __name__ == "__main__":
    unittest.main()
