"""RUST-024, RUST-025, CACHE-018 static clauses -- ci_lint/rules/cook_cache.py (#120)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.cook_cache import check_cache_018, check_rust_024, check_rust_025
from ci_lint.tests.helpers import requires_yaml_tooling

SOLDR = "zackees/setup-soldr@v0"


def _repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="cook-cache-"))
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in files.items():
        (wf / name).write_text(text, encoding="utf-8")
    return root


def _wf(on: str, steps: str) -> str:
    return f"name: ci\non: {on}\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n{steps}"


@requires_yaml_tooling
class Rust024Test(unittest.TestCase):
    def test_rustflags_after_cooking_step_is_violation(self) -> None:
        steps = f'      - uses: {SOLDR}\n      - run: echo "RUSTFLAGS=-Ctarget-cpu=native" >> $GITHUB_ENV\n'
        findings = check_rust_024(_repo({"ci.yml": _wf("[push]", steps)}))
        self.assertEqual(["RUST-024"], [f.rule for f in findings])
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertIn("RUSTFLAGS", findings[0].message)

    def test_step_env_linker_after_cook_is_violation(self) -> None:
        steps = (
            f"      - uses: {SOLDR}\n        with:\n          prebuild-deps: soldr-cook\n"
            "      - run: soldr cargo build\n        env:\n          CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER: x\n"
        )
        findings = check_rust_024(_repo({"ci.yml": _wf("[push]", steps)}))
        self.assertEqual(Status.VIOLATION, findings[0].status)

    def test_expression_prebuild_deps_is_needs_review(self) -> None:
        steps = (
            f"      - uses: {SOLDR}\n        with:\n          prebuild-deps: ${{{{ matrix.cook }}}}\n"
            "      - run: export CC_aarch64_unknown_linux_gnu=gcc\n"
        )
        findings = check_rust_024(_repo({"ci.yml": _wf("[push]", steps)}))
        self.assertEqual(Status.NEEDS_REVIEW, findings[0].status)

    def test_prebuild_none_or_env_before_is_clean(self) -> None:
        none = f"      - uses: {SOLDR}\n        with:\n          prebuild-deps: none\n      - run: export RUSTFLAGS=x\n"
        before = f"      - run: echo RUSTFLAGS=x >> $GITHUB_ENV\n      - uses: {SOLDR}\n"
        self.assertEqual([], check_rust_024(_repo({"a.yml": _wf("[push]", none), "b.yml": _wf("[push]", before)})))


@requires_yaml_tooling
class Rust025Test(unittest.TestCase):
    def test_actions_cache_of_cook_dir_is_violation(self) -> None:
        steps = "      - uses: actions/cache@0123456789abcdef0123456789abcdef01234567\n        with:\n          path: target/cook\n          key: k\n"
        findings = check_rust_025(None, _repo({"ci.yml": _wf("[push]", steps)}))
        self.assertEqual(["RUST-025"], [f.rule for f in findings])
        self.assertEqual(Status.VIOLATION, findings[0].status)

    def test_soldr_cook_save_is_violation(self) -> None:
        findings = check_rust_025(None, _repo({"ci.yml": _wf("[push]", "      - run: soldr cook save\n")}))
        self.assertEqual(["RUST-025"], [f.rule for f in findings])

    def test_unrelated_cache_and_plain_cook_are_clean(self) -> None:
        steps = (
            "      - uses: actions/cache@0123456789abcdef0123456789abcdef01234567\n        with:\n          path: ~/.npm\n          key: npm\n"
            "      - run: soldr cook\n"
        )
        self.assertEqual([], check_rust_025(None, _repo({"ci.yml": _wf("[push]", steps)})))


@requires_yaml_tooling
class Cache018Test(unittest.TestCase):
    def test_pr_only_cook_is_needs_review(self) -> None:
        steps = f"      - uses: {SOLDR}\n        with:\n          cache-key-suffix: cross\n"
        findings = check_cache_018(_repo({"ci.yml": _wf("[pull_request]", steps)}))
        self.assertEqual(["CACHE-018"], [f.rule for f in findings])
        self.assertEqual(Status.NEEDS_REVIEW, findings[0].status)

    def test_matching_push_lane_is_clean(self) -> None:
        steps = f"      - uses: {SOLDR}\n        with:\n          cache-key-suffix: cross\n"
        self.assertEqual([], check_cache_018(_repo({"ci.yml": _wf("[pull_request, push]", steps)})))

    def test_pr_lane_with_prebuild_none_is_clean(self) -> None:
        steps = f"      - uses: {SOLDR}\n        with:\n          prebuild-deps: none\n"
        self.assertEqual([], check_cache_018(_repo({"ci.yml": _wf("[pull_request]", steps)})))

    def test_cook_in_ci_pre_is_violation(self) -> None:
        findings = check_cache_018(_repo({"ci-pre.yml": _wf("[pull_request, push]", f"      - uses: {SOLDR}\n")}))
        self.assertEqual(["CACHE-018"], [f.rule for f in findings])
        self.assertEqual(Status.VIOLATION, findings[0].status)


if __name__ == "__main__":
    unittest.main()
