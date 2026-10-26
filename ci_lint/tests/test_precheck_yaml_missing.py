"""Precheck fails closed when no YAML parser is available.

Without PyYAML (and without yq) every workflow loads as `document=None` and
most workflow rules (GEN-024, CACHE-038, ...) return nothing. Precheck must
say so with one explicit needs_review finding, never report a silent pass.
These tests simulate the missing parser, so they run with or without PyYAML.
"""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_lint import mini_yaml
from ci_lint.finding import Status
from ci_lint.precheck import (
    YAML_DEPENDENT_RULES,
    YAML_TOOLING_RULE,
    run_precheck,
    yaml_unavailable_findings,
)
from ci_lint.tests.helpers import fixture
from ci_lint.yaml_io import yaml_tooling_available


def _no_yaml() -> AbstractContextManager[object]:
    # `sys.modules["yaml"] = None` makes `import yaml` raise ImportError.
    return mock.patch.dict(sys.modules, {"yaml": None})


class YamlMissingTest(unittest.TestCase):
    def test_simulation_really_removes_the_parser(self) -> None:
        with _no_yaml(), mock.patch("ci_lint.yaml_io.shutil.which", return_value=None):
            self.assertFalse(yaml_tooling_available())

    def test_precheck_emits_one_explicit_needs_review(self) -> None:
        with _no_yaml(), mock.patch("ci_lint.yaml_io.shutil.which", return_value=None):
            result = run_precheck(fixture("_e2e", "green"), title="[ci-windows] x")
        hits = [f for f in result.findings if f.rule == YAML_TOOLING_RULE]
        self.assertEqual(1, len(hits))
        self.assertEqual(Status.NEEDS_REVIEW, hits[0].status)
        for rule in ("GEN-024", "CACHE-038"):
            self.assertIn(rule, hits[0].message)
            self.assertIn(rule, YAML_DEPENDENT_RULES)

    def test_no_finding_when_a_parser_is_available(self) -> None:
        self.assertEqual([], yaml_unavailable_findings(fixture("_e2e", "green"), tooling_available=True))

    def test_no_finding_without_workflow_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], yaml_unavailable_findings(Path(tmp), tooling_available=False))

    def test_mini_yaml_cannot_stand_in_for_workflows(self) -> None:
        # Documents why mini_yaml is not the fallback: real workflow shapes.
        with self.assertRaises(mini_yaml.MiniYamlError):
            mini_yaml.parse("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
        with self.assertRaises(mini_yaml.MiniYamlError):
            mini_yaml.parse("jobs:\n  a:\n    steps:\n      - run: |\n          echo hi\n")


if __name__ == "__main__":
    unittest.main()
