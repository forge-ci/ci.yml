"""RUST-027 (ci_lint/rules/rust_doc_lane.py, #116) and RUST-028
(ci_lint/rules/rust_system_packages.py, #84)."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.rust_doc_lane import check_rust_027
from ci_lint.rules.rust_system_packages import check_rust_028
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


@requires_yaml_tooling
class Rust027Test(unittest.TestCase):
    def test_red_no_publishing_member(self) -> None:
        findings = check_rust_027(fixture("RUST-027", "red"))
        # workflow `cargo doc` + ci/lint.py one level in; doctests and RUSTDOCFLAGS are not lanes.
        self.assertEqual([(".github/workflows/ci.yml", None), ("ci/lint.py", 2)], [(f.path, f.line) for f in findings])
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))

    def test_green_allow_comment(self) -> None:
        self.assertEqual([], check_rust_027(fixture("RUST-027", "green-allow")))

    def test_green_not_pr_gate(self) -> None:
        self.assertEqual([], check_rust_027(fixture("RUST-027", "green-release-only")))

    def test_publishing_members_scope(self) -> None:
        findings = check_rust_027(fixture("RUST-027", "publish"))
        got = [(f.message.split(":", 1)[0], f.status) for f in findings]
        self.assertEqual(
            [
                ("jobs.docs.steps[1]", Status.VIOLATION),  # --workspace
                ("jobs.docs.steps[2]", Status.VIOLATION),  # inherits publish = false
                ("jobs.docs.steps[3]", Status.NEEDS_REVIEW),  # no -p
            ],
            got,
        )
        self.assertIn("-p metacrate -p pubcrate", findings[0].fix)


@requires_yaml_tooling
class Rust028Test(unittest.TestCase):
    def test_red_ungated_install(self) -> None:
        findings = check_rust_028(fixture("RUST-028", "red"))
        self.assertEqual(1, len(findings))
        f = findings[0]
        self.assertEqual(Status.NEEDS_REVIEW, f.status)
        self.assertIn("jobs.linux.steps[0]", f.message)
        self.assertIn("libgtk-3-dev", f.message)
        self.assertNotIn("xvfb", f.message)

    def test_green_direct_sys_dep(self) -> None:
        self.assertEqual([], check_rust_028(fixture("RUST-028", "green-direct-sys")))


if __name__ == "__main__":
    unittest.main()
