"""`ci-lint precheck --live` -- ci_lint.cli._live_cache_findings and its
interaction with ci_lint.precheck's LOCAL_SKIPPED_CHECKS (round-4A brief,
deliverable 4). No network: only the "credentials missing" degrade path is
exercised here (matching `_compute_reuse_for_plan`'s existing pattern in
ci_lint/cli.py); ci_lint/tests/test_cache_audit.py covers the live-audit
logic itself with a fake fetch/graphql.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ci_lint.cli import _live_cache_findings
from ci_lint.finding import Status
from ci_lint.precheck import LIVE_COVERED_RULES, run_precheck
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling

REPO = fixture("_e2e", "green")


class LiveCacheFindingsCredentialsTest(unittest.TestCase):
    def test_none_ci_short_circuits_with_no_findings(self) -> None:
        self.assertEqual([], _live_cache_findings(None, default_branch="main"))

    def test_missing_credentials_degrade_to_a_single_needs_review(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            os.environ.pop("GITHUB_REPOSITORY", None)
            out = _live_cache_findings(ci, default_branch="main")
        self.assertEqual(1, len(out))
        self.assertEqual("CACHE-005", out[0].rule)
        self.assertEqual(Status.NEEDS_REVIEW, out[0].status)
        self.assertIn("skipped (--live)", out[0].message)


class PrecheckLiveSuppressesTheStubOnlyForCoveredRules(unittest.TestCase):
    """`--live`'s own findings replace the "not implemented" stub notices
    for CACHE-005/006/008 (they ARE implemented now); ACT-001 is untouched
    -- it audits a different (local act) store this round does not build."""

    @requires_yaml_tooling
    def test_local_and_live_together_drop_only_the_covered_stub_rules(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACT", None)
            result = run_precheck(REPO, title="x", local=True, live=True)
        stub_rules_present = {f.rule for f in result.findings if "skipped (local)" in f.message}
        self.assertEqual(set(), stub_rules_present & LIVE_COVERED_RULES)
        self.assertIn("ACT-001", stub_rules_present)

    @requires_yaml_tooling
    def test_local_without_live_keeps_every_stub(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACT", None)
            result = run_precheck(REPO, title="x", local=True, live=False)
        stub_rules_present = {f.rule for f in result.findings if "skipped (local)" in f.message}
        self.assertTrue(LIVE_COVERED_RULES.issubset(stub_rules_present))


if __name__ == "__main__":
    unittest.main()
