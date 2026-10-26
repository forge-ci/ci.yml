"""GEN-014..018 and CACHE-013 -- ci_lint/rules/ci_pre.py (zackees/ci.yml#23, #51).

Every RED fixture fails with exactly its own rule ID (among group 12's
findings); every GREEN fixture produces no group-12 finding at all.
"""

from __future__ import annotations

import unittest

from ci_lint.rules.ci_pre import carries_pr_number, check_group12
from ci_lint.rules.workflows import check_wf_002
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling
from ci_lint.workflow_scan import load_workflows


def _rules(rule: str, kind: str) -> set[str]:
    repo = fixture(rule, kind)
    _loaded = load_ci_toml(repo)
    ci = _loaded.ci
    findings = _loaded.findings
    assert ci is not None, findings
    return {f.rule for f in check_group12(ci, repo)}


CASES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("GEN-014", ("red",), ("green",)),
    ("GEN-015", ("red",), ("green",)),
    ("GEN-016", ("red", "red-needs"), ("green",)),
    ("GEN-017", ("red", "red-missing"), ("green",)),
    ("GEN-018", ("red", "red-pip"), ("green",)),
    ("CACHE-013", ("red", "red-no-pr"), ("green", "green-event", "green-no-save", "green-restore-only")),
)


class CiPreFixtureTest(unittest.TestCase):
    @requires_yaml_tooling
    def test_red_fails_with_exactly_its_rule(self) -> None:
        for rule, reds, _greens in CASES:
            for kind in reds:
                with self.subTest(rule=rule, kind=kind):
                    self.assertEqual({rule}, _rules(rule, kind))

    @requires_yaml_tooling
    def test_green_has_no_group_12_finding(self) -> None:
        for rule, _reds, greens in CASES:
            for kind in greens:
                with self.subTest(rule=rule, kind=kind):
                    self.assertEqual(set(), _rules(rule, kind))

    @requires_yaml_tooling
    def test_e2e_green_is_clean(self) -> None:
        self.assertEqual(set(), _rules("_e2e", "green"))

    @requires_yaml_tooling
    def test_wf_002_exempts_ci_pre_only(self) -> None:
        """#23 §3: ci-pre.yml has no workflow-level concurrency by design;
        every other workflow still needs one (WF-002)."""

        wfs = load_workflows(fixture("GEN-015", "green"))
        self.assertEqual([], check_wf_002(wfs))
        wfs = load_workflows(fixture("CACHE-013", "green"))
        self.assertEqual([], [f for f in check_wf_002(wfs) if f.path.endswith("ci.yml")])


class CarriesPrNumberTest(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertTrue(carries_pr_number("${{ needs.precheck.outputs.cache_key_pr }}", is_composite=False))
        self.assertTrue(carries_pr_number("x-${{ github.event.pull_request.number }}", is_composite=False))
        self.assertTrue(carries_pr_number("${{ inputs.cache-key-suffix }}", is_composite=True))
        self.assertFalse(carries_pr_number("${{ inputs.cache-key-suffix }}", is_composite=False))
        self.assertFalse(carries_pr_number("${{ github.ref_name }}", is_composite=False))
        self.assertFalse(carries_pr_number(None, is_composite=False))


if __name__ == "__main__":
    unittest.main()
