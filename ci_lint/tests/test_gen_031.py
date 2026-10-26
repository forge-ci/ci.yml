"""GEN-031 -- ci_lint/rules/full_label.py (forge-ci/ci.yml#364)."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.full_label import check_gen_031
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


@requires_yaml_tooling
class Gen031Test(unittest.TestCase):
    def test_green_cancels_and_consumes_label(self) -> None:
        self.assertEqual([], check_gen_031(fixture("GEN-031", "green")))

    def test_green_labeled_only_trigger(self) -> None:
        self.assertEqual([], check_gen_031(fixture("GEN-031", "green-nosync")))

    def test_red_no_cancel_and_sticky_label(self) -> None:
        findings = check_gen_031(fixture("GEN-031", "red"))
        self.assertEqual(2, len(findings))
        self.assertTrue(all(f.rule == "GEN-031" and f.status == Status.VIOLATION for f in findings))
        self.assertIn("concurrency", findings[0].message)
        self.assertIn("synchronize", findings[1].message)


if __name__ == "__main__":
    unittest.main()
