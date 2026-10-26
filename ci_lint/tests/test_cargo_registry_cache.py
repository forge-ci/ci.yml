"""CACHE-037 static clauses (#121): cargo registry cache payload and key."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.cargo_registry_cache import cargo_home_subpath, check_cache_037
from ci_lint.tests.helpers import requires_yaml_tooling

_WF = """name: ci
on: [push]
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: {uses}
        with:
{with_}
"""


def _run(uses: str, with_: str) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text(_WF.format(uses=uses, with_=with_), encoding="utf-8")
        return check_cache_037(None, root)  # type: ignore[arg-type]


GOOD_PATHS = """          path: |
            ~/.cargo/registry/index
            ~/.cargo/registry/cache
            ~/.cargo/.global-cache
            ~/.cargo/git/db
"""
GOOD_KEY = "          key: reg-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('**/Cargo.lock') }}\n"


class CargoHomeSubpathTest(unittest.TestCase):
    def test_subpaths(self) -> None:
        self.assertEqual("registry/src", cargo_home_subpath("~/.cargo/registry/src"))
        self.assertEqual("", cargo_home_subpath("~/.cargo"))
        self.assertEqual("git/db", cargo_home_subpath("${{ env.CARGO_HOME }}/git/db"))
        self.assertIsNone(cargo_home_subpath("target/debug"))


class Cache037Test(unittest.TestCase):
    @requires_yaml_tooling
    def test_green_downloads_only_and_lock_key(self) -> None:
        self.assertEqual([], _run("actions/cache@v4", GOOD_PATHS + GOOD_KEY))

    @requires_yaml_tooling
    def test_red_src_checkouts_whole_home_bin(self) -> None:
        paths = "          path: |\n            ~/.cargo/registry/src\n            ~/.cargo/git/checkouts\n            ~/.cargo/bin\n            ~/.cargo\n"
        findings = _run("actions/cache@v4", paths + GOOD_KEY)
        self.assertEqual(4, len(findings))
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))

    @requires_yaml_tooling
    def test_red_key_missing_arch_and_lock(self) -> None:
        findings = _run("actions/cache/save@v4", GOOD_PATHS + "          key: reg-${{ runner.os }}\n")
        self.assertEqual(1, len(findings))
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertIn("runner.arch", findings[0].message)

    @requires_yaml_tooling
    def test_red_key_hashes_toolchain_file(self) -> None:
        key = "          key: r-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('**/Cargo.lock', 'rust-toolchain.toml') }}\n"
        findings = _run("actions/cache@v4", GOOD_PATHS + key)
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])

    @requires_yaml_tooling
    def test_unresolved_expression_needs_review(self) -> None:
        findings = _run("actions/cache@v4", GOOD_PATHS + "          key: ${{ matrix.key }}\n")
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    @requires_yaml_tooling
    def test_setup_soldr_registry_cache_needs_review(self) -> None:
        findings = _run("zackees/setup-soldr@v0", "          cargo-registry-cache: true\n")
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])
        self.assertEqual([], _run("zackees/setup-soldr@v0", "          cache: true\n"))


if __name__ == "__main__":
    unittest.main()
