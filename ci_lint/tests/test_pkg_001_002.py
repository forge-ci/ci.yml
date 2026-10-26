"""PKG-001 (Linux wheel install smoke in the quick gate) and PKG-002 (npm
tarball smoke, only when package.json exists) -- round M2-20, zackees/ci.yml#44
part A."""

from __future__ import annotations

import unittest

from ci_lint.rules.packaging import check_pkg_001, check_pkg_002
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class Pkg001Test(unittest.TestCase):
    def test_pkg_001_flow_pr_missing_wheel_smoke_suite(self) -> None:
        repo = fixture("PKG-001", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("PKG-001", [f.rule for f in check_pkg_001(ci)])

        repo = fixture("PKG-001", "green")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], check_pkg_001(ci))


class Pkg002Test(unittest.TestCase):
    @requires_yaml_tooling
    def test_pkg_002_missing_npm_pack_install_smoke(self) -> None:
        self.assertIn("PKG-002", [f.rule for f in check_pkg_002(fixture("PKG-002", "red"))])

    @requires_yaml_tooling
    def test_pkg_002_green_has_pack_and_install(self) -> None:
        self.assertEqual([], check_pkg_002(fixture("PKG-002", "green")))

    def test_pkg_002_no_op_without_package_json(self) -> None:
        # A repo with no package.json at all (e.g. the BIN-001 fixture, a
        # pure-Rust/Python repo) must never be flagged -- PKG-002 applies
        # only when package.json exists.
        self.assertEqual([], check_pkg_002(fixture("BIN-001", "red")))


if __name__ == "__main__":
    unittest.main()
