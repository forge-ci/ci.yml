"""RUST-022 / CACHE-015 / CACHE-016 static clauses -- ci_lint/rules/lint_cache.py (#110)."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from ci_lint.rules.lint_cache import check_lint_cache
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import requires_yaml_tooling

_BASE = (
    'schema = 3\nprofile = "library"\n'
    '[platforms]\nlinux-x64 = { target = "x86_64-unknown-linux-gnu", runs-on = "ubuntu-24.04", group = "linux" }\n'
    '[suites]\nunit = { run = "pytest", required = true }\n'
    '[cache]\nbudget = "9GB"\nwrite-on = ["main"]\n'
    "[cache.family]\n@FAMILIES@\n"
    '[allow]\nworkflows = { "ci.yml" = ["push:main"] }\n'
)
_SMALL = 'small = { via = "setup-uv", max = "5MB", per = "none" }'


def _job(name: str, with_: str = "", concurrency: str = "") -> str:
    return (
        f"  {name}:\n    runs-on: ubuntu-24.04\n{concurrency}    steps:\n"
        f"      - uses: zackees/setup-soldr@v0\n        with:\n          dylint: true\n{with_}"
    )


class LintCacheTest(unittest.TestCase):
    def _check(self, workflow: str, families: str = _SMALL) -> list[tuple[str, str]]:
        root = pathlib.Path(tempfile.mkdtemp(prefix="lintcache-"))
        (root / "ci.toml").write_text(_BASE.replace("@FAMILIES@", families), encoding="utf-8")
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(workflow, encoding="utf-8")
        ci = load_ci_toml(root).ci
        return sorted((f.rule, f.status.value) for f in check_lint_cache(ci, root))

    CLEAN = "on: [push, schedule]\njobs:\n" + _job("lint")

    @requires_yaml_tooling
    def test_clean_writer_with_nightly_is_clean(self) -> None:
        self.assertEqual(self._check(self.CLEAN), [])

    @requires_yaml_tooling
    def test_no_dylint_is_clean(self) -> None:
        wf = "on: [push]\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: zackees/setup-soldr@v0\n"
        self.assertEqual(self._check(wf), [])

    @requires_yaml_tooling
    def test_rust_022_full_output_family_and_input(self) -> None:
        fam = _SMALL + '\nout = { via = "setup-soldr:dylint-output", max = "1GB", per = "none", evict = "lru" }'
        self.assertEqual(self._check(self.CLEAN, fam), [("RUST-022", "needs_review")])
        wf = "on: [push, schedule]\njobs:\n" + _job("lint", "          dylint-output-cache: true\n")
        self.assertEqual(self._check(wf), [("RUST-022", "needs_review")])

    @requires_yaml_tooling
    def test_rust_022_lockfile_in_prefix(self) -> None:
        fam = _SMALL + '\nlint = { prefix = "lint-deps-cargo-lock-", max = "1GB", per = "none", evict = "lru" }'
        self.assertIn(("RUST-022", "needs_review"), self._check(self.CLEAN, fam))
        wf = self.CLEAN + (
            "      - uses: actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809\n        with:\n"
            "          path: target\n          key: lint-${{ hashFiles('Cargo.lock') }}\n"
            "          restore-keys: lint-${{ hashFiles('**/Cargo.toml') }}-\n"
        )
        self.assertIn(("RUST-022", "needs_review"), self._check(wf))

    @requires_yaml_tooling
    def test_rust_022_incremental_and_compression(self) -> None:
        wf = "on: [push, schedule]\njobs:\n" + _job("lint", "          target-cache-include-incremental: true\n")
        self.assertEqual(self._check(wf), [("RUST-022", "violation")])
        wf = "on: [push, schedule]\njobs:\n" + _job("lint", "          target-cache-compress: none\n")
        self.assertEqual(self._check(wf), [("RUST-022", "violation")])
        wf = "on: [push, schedule]\njobs:\n" + _job("lint", '          target-cache-compress-level: "1"\n')
        self.assertEqual(self._check(wf), [("RUST-022", "violation")])
        wf = "on: [push, schedule]\njobs:\n" + _job("lint", '          target-cache-compress-level: "19"\n')
        self.assertEqual(self._check(wf), [("RUST-022", "needs_review")])
        wf = "on: [push, schedule]\njobs:\n" + _job("lint", '          target-cache-compress-level: "9"\n')
        self.assertEqual(self._check(wf), [])

    @requires_yaml_tooling
    def test_cache_015_hand_rolled_dylint_target(self) -> None:
        wf = self.CLEAN + (
            "      - uses: actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809\n        with:\n"
            "          path: target/dylint\n          key: dylint-target-v3-${{ runner.os }}\n"
        )
        self.assertEqual(self._check(wf), [("CACHE-015", "violation")])

    @requires_yaml_tooling
    def test_cache_016_cancellable_writer(self) -> None:
        wf = "on: [push]\nconcurrency:\n  group: ci\n  cancel-in-progress: true\njobs:\n" + _job("lint")
        self.assertEqual(self._check(wf), [("CACHE-016", "needs_review"), ("CACHE-016", "violation")])
        wf = (
            "on: [push]\nconcurrency:\n  group: ci\n  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n"
            "jobs:\n" + _job("lint")
        )
        self.assertEqual(self._check(wf), [("CACHE-016", "needs_review"), ("CACHE-016", "needs_review")])

    @requires_yaml_tooling
    def test_cache_016_missing_nightly(self) -> None:
        wf = "on: [push]\njobs:\n" + _job("lint")
        self.assertEqual(self._check(wf), [("CACHE-016", "needs_review")])

    @requires_yaml_tooling
    def test_cache_016_restore_only_is_left_to_cache_027(self) -> None:
        wf = "on: [push]\njobs:\n" + _job("lint", '          save-cache: "false"\n')
        self.assertEqual(self._check(wf), [])


if __name__ == "__main__":
    unittest.main()


def _cache_job(paths: str, extra_run: str = "") -> str:
    return (
        "on: [push]\njobs:\n  lint:\n    runs-on: ubuntu-24.04\n    steps:\n"
        f"{extra_run}"
        "      - uses: actions/cache@v4\n        with:\n          key: dylint-deps-v1-x\n"
        f"          path: |\n{paths}"
    )


class LintCachePayloadTest(unittest.TestCase):
    _check = LintCacheTest._check
    @requires_yaml_tooling
    def test_incremental_path_is_violation(self) -> None:
        wf = _cache_job("            target/debug/incremental\n")
        self.assertEqual(self._check(wf), [("RUST-022", "violation")])

    @requires_yaml_tooling
    def test_workspace_tree_without_freshness_needs_review(self) -> None:
        self.assertEqual(self._check(_cache_job("            target\n")), [("RUST-022", "needs_review")])

    @requires_yaml_tooling
    def test_workspace_tree_with_freshness_is_clean(self) -> None:
        run = "      - run: soldr dylint --all -- -Zchecksum-freshness\n"
        self.assertEqual(self._check(_cache_job("            target/debug\n", run)), [])

    @requires_yaml_tooling
    def test_lint_library_trees_are_exempt(self) -> None:
        wf = _cache_job("            target/dylint/libraries\n            target/dylint/target\n")
        self.assertEqual(self._check(wf), [])
