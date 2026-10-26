"""BIN-001, BIN-002 -- ci_lint/rules/bin_artifacts.py. Pure ci.toml
inspection; no YAML tooling needed."""

from __future__ import annotations

import dataclasses
import unittest

from ci_lint.rules.bin_artifacts import check_bin_001, check_bin_002
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture


class BinArtifactsFixtureTest(unittest.TestCase):
    def test_bin_001_missing_musl_floor(self) -> None:
        repo = fixture("BIN-001", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("BIN-001", [f.rule for f in check_bin_001(ci)])

        repo = fixture("BIN-001", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("BIN-001", [f.rule for f in check_bin_001(ci)])

    def test_bin_002_gnu_windows_abi(self) -> None:
        repo = fixture("BIN-002", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("BIN-002", [f.rule for f in check_bin_002(ci)])

        repo = fixture("BIN-002", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("BIN-002", [f.rule for f in check_bin_002(ci)])

    def test_bin_rules_are_no_ops_without_a_native_cli(self) -> None:
        repo = fixture("BIN-001", "red")
        ci = load_ci_toml(repo).ci
        assert ci is not None and ci.python is not None
        non_native = dataclasses.replace(ci.python, cli=dataclasses.replace(ci.python.cli, native=False))
        ci_non_native = dataclasses.replace(ci, python=non_native)
        self.assertEqual([], check_bin_001(ci_non_native))
        self.assertEqual([], check_bin_002(ci_non_native))


if __name__ == "__main__":
    unittest.main()
