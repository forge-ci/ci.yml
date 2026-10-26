"""RUST-013 -- float soldr by default (zackees/ci.yml#18).

An exact `soldr==` pin in pyproject.toml's [build-system].requires, or a
literal `version:` input on a `zackees/setup-soldr` step, is a violation
unless a matching `[[exceptions]]` entry exists (checked through the
generic `ci_lint.exceptions.apply_exceptions` mechanism, keyed on
(rule, path)).
"""

from __future__ import annotations

import unittest

from ci_lint.exceptions import apply_exceptions
from ci_lint.rules.rust_units import check_group7
from ci_lint.rules.soldr_pin import check_rust_013
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class Rust013FixtureTest(unittest.TestCase):
    def test_exact_pyproject_pin_is_a_violation(self) -> None:
        repo = fixture("RUST-013", "red-pyproject-pin")
        ci = load_ci_toml(repo).ci
        findings = check_rust_013(ci, repo)
        self.assertIn("RUST-013", [f.rule for f in findings])
        pin_findings = [f for f in findings if f.rule == "RUST-013" and f.path == "pyproject.toml"]
        self.assertTrue(pin_findings, "expected a RUST-013 finding at pyproject.toml")

    def test_floor_pin_pyproject_is_clean(self) -> None:
        repo = fixture("RUST-013", "green-pyproject-floor")
        ci = load_ci_toml(repo).ci
        findings = check_rust_013(ci, repo)
        self.assertNotIn("RUST-013", [f.rule for f in findings])

    @requires_yaml_tooling
    def test_setup_soldr_literal_version_is_a_violation(self) -> None:
        repo = fixture("RUST-013", "red-setup-soldr-version")
        ci = load_ci_toml(repo).ci
        findings = check_rust_013(ci, repo)
        self.assertIn("RUST-013", [f.rule for f in findings])
        version_findings = [
            f for f in findings if f.rule == "RUST-013" and f.path.endswith("action.yml")
        ]
        self.assertTrue(version_findings, "expected a RUST-013 finding at the wrapper action.yml")

    @requires_yaml_tooling
    def test_setup_soldr_without_version_is_clean(self) -> None:
        repo = fixture("RUST-013", "green-setup-soldr-no-version")
        ci = load_ci_toml(repo).ci
        findings = check_rust_013(ci, repo)
        self.assertNotIn("RUST-013", [f.rule for f in findings])

    def test_recorded_exception_waives_the_pyproject_pin(self) -> None:
        repo = fixture("RUST-013", "green-exception")
        ci = load_ci_toml(repo).ci
        raw_findings = check_rust_013(ci, repo)
        self.assertIn("RUST-013", [f.rule for f in raw_findings], "sanity: rule still fires raw")

        outcome = apply_exceptions(ci, raw_findings)
        remaining_violations = [
            f.rule for f in outcome.findings if f.status.value == "violation"
        ]
        self.assertNotIn("RUST-013", remaining_violations)
        self.assertTrue(
            any(f.rule == "RUST-013" for f in outcome.findings),
            "the finding must survive as approved_exception, not disappear",
        )

    def test_wired_into_group7(self) -> None:
        repo = fixture("RUST-013", "red-pyproject-pin")
        ci = load_ci_toml(repo).ci
        self.assertIn("RUST-013", [f.rule for f in check_group7(ci, repo)])


if __name__ == "__main__":
    unittest.main()
