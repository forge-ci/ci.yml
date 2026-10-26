"""RUST-017 -- ci_lint/rules/rust_nocapture.py (zackees/ci.yml#79, M2-40)."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.rust_nocapture import check_rust_017
from ci_lint.rules.test_invocations import with_yaml_comment
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


@requires_yaml_tooling
class Rust017Test(unittest.TestCase):
    def test_green_is_clean(self) -> None:
        self.assertEqual([], check_rust_017(fixture("RUST-017", "green")))

    def test_green_same_line_allow_comment(self) -> None:
        self.assertEqual([], check_rust_017(fixture("RUST-017", "green-allow")))

    def test_green_single_named_test(self) -> None:
        # ci.yml#136: a name filter plus --ignored/--exact selects one test.
        self.assertEqual([], check_rust_017(fixture("RUST-017", "green-single-test")))

    def test_red_filter_without_exact_or_ignored(self) -> None:
        findings = check_rust_017(fixture("RUST-017", "red-filter"))
        self.assertEqual(["RUST-017", "RUST-017"], [f.rule for f in findings])
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))

    def test_red_run_lines(self) -> None:
        findings = check_rust_017(fixture("RUST-017", "red"))
        self.assertEqual(["RUST-017", "RUST-017"], [f.rule for f in findings])
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))
        text = " ".join(f.message for f in findings)
        self.assertIn("--no-capture", text)
        self.assertIn("--test-threads 1", text)

    def test_red_one_level_into_ci_script(self) -> None:
        findings = check_rust_017(fixture("RUST-017", "red-script"))
        self.assertEqual(1, len(findings))
        f = findings[0]
        self.assertEqual(("RUST-017", "ci/test.py", 3, Status.VIOLATION), (f.rule, f.path, f.line, f.status))

    def test_dynamic_append_is_needs_review(self) -> None:
        findings = check_rust_017(fixture("RUST-017", "review-script"))
        self.assertEqual(1, len(findings))
        self.assertEqual(Status.NEEDS_REVIEW, findings[0].status)
        self.assertEqual(6, findings[0].line)

    def test_module_reference_and_cargo_helper_is_needs_review(self) -> None:
        # running-process: `uv run --module ci.independent_spawn`, argv built by
        # cargo_command("test", ...) with --nocapture held in a list literal.
        findings = check_rust_017(fixture("RUST-017", "review-module-helper"))
        self.assertEqual(
            [("ci/independent_spawn.py", 10, Status.NEEDS_REVIEW)],
            [(f.path, f.line, f.status) for f in findings],
        )

    def test_short_command_does_not_borrow_another_steps_line(self) -> None:
        # zccache test-action.yml: `fi` is a substring of `fingerprint-bin`, so the
        # YAML-comment recovery used to hand the `fi` step the whole cargo test line
        # and the one offender was reported once per such step.
        findings = check_rust_017(fixture("RUST-017", "red-borrowed-comment"))
        self.assertEqual(["jobs.t.steps[2]"], [f.message.split(":", 1)[0] for f in findings])

    def test_with_yaml_comment_needs_whole_command(self) -> None:
        raw = ["        run: cargo test --features fingerprint-bin  # ci-lint: allow X why"]
        self.assertEqual("fi", with_yaml_comment("fi", raw))
        self.assertEqual("exit 1", with_yaml_comment("exit 1", raw))
        self.assertIn("allow X why", with_yaml_comment("cargo test --features fingerprint-bin", raw))
        block = ["          cargo test  # ci-lint: allow X why"]
        self.assertEqual("cargo test  # ci-lint: allow X why", with_yaml_comment("cargo test", block))


if __name__ == "__main__":
    unittest.main()
