"""GEN-007, RUST-008, RUST-010 -- ci_lint/rules/m2_22.py (M2-22, ci.yml#52
part of #44/#1, issue #5's RUST-008/010 and the case-study GEN-007
candidate)."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.m2_22 import check_gen_007, check_rust_008, check_rust_010
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]


@requires_yaml_tooling
class Gen007Test(unittest.TestCase):
    def test_unscoped_paths_with_large_matrix_is_needs_review(self) -> None:
        findings = check_gen_007(fixture("GEN-007", "red"))
        self.assertIn("GEN-007", rule_ids(findings))
        self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in findings))
        self.assertTrue(any("no 'paths:' filter" in f.message for f in findings))

    def test_scoped_paths_is_clean(self) -> None:
        findings = check_gen_007(fixture("GEN-007", "green"))
        self.assertEqual([], findings)


@requires_yaml_tooling
class Rust008Test(unittest.TestCase):
    def test_no_check_only_coverage_is_violation(self) -> None:
        repo = fixture("RUST-008", "red")
        ci = load_ci_toml(repo).ci
        findings = check_rust_008(ci, repo)
        self.assertIn("RUST-008", rule_ids(findings))
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))
        self.assertTrue(any("x86_64-unknown-linux-gnu" in f.message for f in findings))
        self.assertTrue(any("x86_64-pc-windows-msvc" in f.message for f in findings))

    def test_full_check_only_coverage_is_clean(self) -> None:
        repo = fixture("RUST-008", "green-check-coverage")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], check_rust_008(ci, repo))

    def test_boundary_lint_exempts_repo(self) -> None:
        repo = fixture("RUST-008", "green-boundary-lint")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], check_rust_008(ci, repo))


@requires_yaml_tooling
class Rust010Test(unittest.TestCase):
    def test_non_linux_cook_is_violation(self) -> None:
        findings = check_rust_010(fixture("RUST-010", "red-non-linux"))
        self.assertIn("RUST-010", rule_ids(findings))
        self.assertTrue(any(f.status == Status.VIOLATION for f in findings))
        self.assertTrue(any("macos-15" in f.message for f in findings))

    def test_missing_default_branch_guard_is_needs_review(self) -> None:
        findings = check_rust_010(fixture("RUST-010", "red-no-guard"))
        self.assertIn("RUST-010", rule_ids(findings))
        self.assertTrue(any(f.status == Status.NEEDS_REVIEW and "default-branch guard" in f.message for f in findings))

    def test_post_build_capture_is_needs_review(self) -> None:
        findings = check_rust_010(fixture("RUST-010", "red-post-build"))
        self.assertIn("RUST-010", rule_ids(findings))
        self.assertTrue(any("after a build/test step" in f.message for f in findings))

    def test_green_linux_guarded_pre_build_save_is_clean(self) -> None:
        findings = check_rust_010(fixture("RUST-010", "green"))
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
