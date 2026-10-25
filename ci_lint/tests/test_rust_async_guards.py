"""RUST-019 static settings check (zackees/ci.yml#413).

RED/GREEN fixtures for the enforced clauses: workspace (or lone package)
deny on all three guard lints, member inheritance without weakening, and
clippy.toml well-formedness. The Candidate clauses (custom-type coverage
completeness, large_futures thresholds) are deliberately not asserted
here -- policy-rust.md records them as Candidate.
"""

from __future__ import annotations

import unittest

from ci_lint.rules.rust_async_guards import GUARD_LINTS, check_rust_019
from ci_lint.tests.test_complexity import _Repo

GOOD_WORKSPACE = """\
[workspace]
members = ["crates/*"]

[workspace.lints.clippy]
await_holding_lock = "deny"
await_holding_refcell_ref = "deny"
await_holding_invalid_type = "deny"
"""

MEMBER_INHERITS = """\
[package]
name = "a"
version = "0.1.0"

[lints]
workspace = true
"""

MEMBER_WEAKENS = """\
[package]
name = "b"
version = "0.1.0"

[lints]
workspace = true

[lints.clippy]
await_holding_lock = "warn"
"""

GOOD_CLIPPY_TOML = """\
await-holding-invalid-types = [
  { path = "my_crate::sync::MutexGuard", reason = "blocking guard: release it before .await" },
]
"""


def _where(findings) -> list[str]:
    return [f.location() for f in findings]


class Rust019SettingsTest(unittest.TestCase):
    def test_green_workspace_with_inheriting_member(self) -> None:
        with _Repo({"Cargo.toml": GOOD_WORKSPACE, "crates/a/Cargo.toml": MEMBER_INHERITS}) as root:
            self.assertEqual([], check_rust_019(root))

    def test_green_forbid_is_stronger_than_deny(self) -> None:
        manifest = GOOD_WORKSPACE.replace('"deny"', '"forbid"')
        with _Repo({"Cargo.toml": manifest, "crates/a/Cargo.toml": MEMBER_INHERITS}) as root:
            self.assertEqual([], check_rust_019(root))

    def test_green_soldr_shape_with_registered_custom_guards(self) -> None:
        # The soldr#3856 adoption shape: three denies + clippy.toml listing
        # ONLY the repo's own wrappers (std types are the built-ins' job).
        with _Repo(
            {"Cargo.toml": GOOD_WORKSPACE, "crates/a/Cargo.toml": MEMBER_INHERITS, "clippy.toml": GOOD_CLIPPY_TOML}
        ) as root:
            self.assertEqual([], check_rust_019(root))

    def test_missing_guard_lints_are_violations(self) -> None:
        with _Repo({"Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n'}) as root:
            findings = check_rust_019(root)
        self.assertEqual(len(GUARD_LINTS), len(findings))
        self.assertTrue(all(f.rule == "RUST-019" for f in findings))
        self.assertIn("await_holding_lock", findings[0].message)

    def test_warn_is_not_deny(self) -> None:
        manifest = GOOD_WORKSPACE.replace('await_holding_lock = "deny"', 'await_holding_lock = "warn"')
        with _Repo({"Cargo.toml": manifest, "crates/a/Cargo.toml": MEMBER_INHERITS}) as root:
            findings = check_rust_019(root)
        self.assertEqual(1, len(findings))
        self.assertIn("`warn`", findings[0].message)
        self.assertIn("await_holding_lock", findings[0].message)

    def test_lone_package_uses_lints_clippy(self) -> None:
        manifest = (
            '[package]\nname = "x"\nversion = "0.1.0"\n\n[lints.clippy]\n'
            'await_holding_lock = "deny"\nawait_holding_refcell_ref = "deny"\n'
            'await_holding_invalid_type = "deny"\n'
        )
        with _Repo({"Cargo.toml": manifest}) as root:
            self.assertEqual([], check_rust_019(root))
        with _Repo({"Cargo.toml": manifest.replace('await_holding_lock = "deny"\n', "")}) as root:
            findings = check_rust_019(root)
        self.assertEqual(["Cargo.toml"], _where(findings))
        self.assertIn("[lints.clippy]", findings[0].message)

    def test_member_must_inherit(self) -> None:
        lone_member = '[package]\nname = "b"\nversion = "0.1.0"\n'
        with _Repo({"Cargo.toml": GOOD_WORKSPACE, "crates/b/Cargo.toml": lone_member}) as root:
            findings = check_rust_019(root)
        self.assertEqual(["crates/b/Cargo.toml"], _where(findings))
        self.assertIn("workspace = true", findings[0].message)

    def test_member_weakening_is_a_violation(self) -> None:
        with _Repo({"Cargo.toml": GOOD_WORKSPACE, "crates/b/Cargo.toml": MEMBER_WEAKENS}) as root:
            findings = check_rust_019(root)
        self.assertEqual(["crates/b/Cargo.toml"], _where(findings))
        self.assertIn("re-declares", findings[0].message)
        self.assertIn("await_holding_lock", findings[0].message)

    def test_clippy_toml_well_formedness_only(self) -> None:
        # Absent key: fine (registration completeness is Candidate).
        with _Repo({"Cargo.toml": GOOD_WORKSPACE, "crates/a/Cargo.toml": MEMBER_INHERITS, "clippy.toml": "# none\n"}) as root:
            self.assertEqual([], check_rust_019(root))
        # Present but malformed: violation.
        bad = "await-holding-invalid-types = [{ path = \"x::Guard\" }]\n"
        with _Repo({"Cargo.toml": GOOD_WORKSPACE, "crates/a/Cargo.toml": MEMBER_INHERITS, "clippy.toml": bad}) as root:
            findings = check_rust_019(root)
        self.assertEqual(["clippy.toml"], _where(findings))
        self.assertIn("reason", findings[0].message)
        not_a_list = 'await-holding-invalid-types = "x::Guard"\n'
        with _Repo({"Cargo.toml": GOOD_WORKSPACE, "crates/a/Cargo.toml": MEMBER_INHERITS, "clippy.toml": not_a_list}) as root:
            findings = check_rust_019(root)
        self.assertEqual(["clippy.toml"], _where(findings))

    def test_not_a_rust_repo(self) -> None:
        with _Repo({"README.md": "hi\n"}) as root:
            self.assertEqual([], check_rust_019(root))