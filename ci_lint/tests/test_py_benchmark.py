"""PY-001 -- ci_lint/rules/py_benchmark.py (M2-21, ci.yml#44 part B).

AST scan of `[suites.perf].run`'s declared entry point(s) for raw
dict/`Any` usage instead of dataclasses.
"""

from __future__ import annotations

import unittest

from ci_lint.rules.py_benchmark import check_group14
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


@requires_yaml_tooling
class Py001Test(unittest.TestCase):
    def test_red_raw_dict_and_any(self) -> None:
        repo = fixture("PY-001", "red")
        ci = load_ci_toml(repo).ci
        assert ci is not None
        findings = check_group14(ci, repo)
        rules = [f.rule for f in findings]
        self.assertIn("PY-001", rules)
        self.assertGreaterEqual(len(findings), 2, msg=findings)

    def test_green_dataclass(self) -> None:
        repo = fixture("PY-001", "green")
        ci = load_ci_toml(repo).ci
        assert ci is not None
        findings = check_group14(ci, repo)
        self.assertEqual([], findings, msg=findings)


if __name__ == "__main__":
    unittest.main()
