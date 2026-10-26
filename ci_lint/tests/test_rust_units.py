"""RUST-005, RUST-011, RUST-012 -- ci_lint/cargo_scan.py + rules/rust_units.py.

No cargo is invoked; targets are derived from tomllib + the filesystem.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.cargo_scan import discover_workspace
from ci_lint.rules.rust_units import check_rust_005, check_rust_011, check_rust_012
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class RustUnitsFixtureTest(unittest.TestCase):
    def test_rust_012_zero_test_lib_harness(self) -> None:
        repo = fixture("RUST-012", "red")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertEqual(1, len(crates), "fixture must resolve exactly one crate")
        self.assertIn("RUST-012", [f.rule for f in check_rust_012(ci, crates, repo)])

        repo = fixture("RUST-012", "green")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertNotIn("RUST-012", [f.rule for f in check_rust_012(ci, crates, repo)])

    def test_rust_005_integration_test_overrun(self) -> None:
        repo = fixture("RUST-005", "red")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertIn("RUST-005", [f.rule for f in check_rust_005(ci, crates)])

        repo = fixture("RUST-005", "green")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertNotIn("RUST-005", [f.rule for f in check_rust_005(ci, crates)])

    def test_rust_011_private_crate_without_publish_false(self) -> None:
        repo = fixture("RUST-011", "red")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertIn("RUST-011", [f.rule for f in check_rust_011(ci, crates, repo)])

        repo = fixture("RUST-011", "green")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertNotIn("RUST-011", [f.rule for f in check_rust_011(ci, crates, repo)])

    @requires_yaml_tooling  # the --features value is read from parsed workflow YAML
    def test_rust_011_features_default_on_empty_default_matches_empty_ship_set(self) -> None:
        """zackees/ci.yml#89: `--features default` names the empty graph when
        the public crate's default is empty/absent (GREEN), while a feature
        outside [rust].ship = [[]] is still RUST-011 (RED)."""

        import shutil

        for features, expect in (("default", False), ("extra", True)):
            with self.subTest(features=features), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                shutil.copytree(fixture("RUST-011", "green"), repo)
                wf = repo / ".github" / "workflows"
                wf.mkdir(parents=True, exist_ok=True)
                (wf / "unit.yml").write_text(
                    "on: [push]\njobs:\n  t:\n    runs-on: ubuntu-24.04\n    steps:\n"
                    f"      - run: cargo test --features {features}\n",
                    encoding="utf-8",
                )
                ci = load_ci_toml(repo).ci
                crates = discover_workspace(repo)
                msgs = [f.message for f in check_rust_011(ci, crates, repo) if "--features" in f.message]
                self.assertEqual(expect, bool(msgs), msgs)

    def test_rust_011_doc_comment_mentioning_cfg_feature_is_not_a_violation(self) -> None:
        """Regression for round-2A defect 2: `crates/private/template-json/
        src/lib.rs` in the real template repo has a doc comment mentioning
        `cfg(feature = "json")` to explain the amalgam-wiring pattern; the
        crate itself has no real `cfg(feature = ...)`. `has_cfg_feature`
        must ignore comment content."""

        repo = fixture("RUST-011", "green-doc-comment")
        ci = load_ci_toml(repo).ci
        crates = discover_workspace(repo)
        self.assertEqual([], check_rust_011(ci, crates, repo))


class CargoScanUnitTest(unittest.TestCase):
    def test_expected_target_names_naming_scheme(self) -> None:
        from ci_lint.cargo_scan import expected_target_names

        repo = fixture("RUST-012", "green")
        crates = discover_workspace(repo)
        names = expected_target_names(crates[0])
        self.assertEqual({"demo-core:lib": "lib"}, {t.name: t.kind for t in names})


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_WORKSPACE_MANIFEST = '[workspace]\nmembers = ["crates/private/demo-core"]\n'
_PRIVATE_MANIFEST = (
    '[package]\nname = "demo-core"\nversion = "0.1.0"\nedition = "2021"\npublish = false\n'
)


class RustUnitsMultilineCfgFeatureTest(unittest.TestCase):
    """Round-6B regression (found on zackees/template-python-rust-cmd,
    round 2F): `cargo_scan.has_cfg_feature` matched the literal substring
    `"cfg(feature"` / `"cfg_attr(feature"` against the whole (comment/
    string-stripped) file text, so splitting the paren and `feature`
    across lines -- `#[cfg(\n    feature = "json"\n)]` -- evaded RUST-011
    the same way it evaded LAYOUT-001. Each case builds a throwaway
    workspace + private-crate manifest (matching `[rust].private =
    "crates/private/*"`) on disk and reuses the RUST-011 green fixture's
    `ci.toml`, so only `has_cfg_feature` -- not `publish = false` /
    `[features]` -- is under test."""

    def _findings(self, lib_rs: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "Cargo.toml", _WORKSPACE_MANIFEST)
            _write(root, "crates/private/demo-core/Cargo.toml", _PRIVATE_MANIFEST)
            _write(root, "crates/private/demo-core/src/lib.rs", lib_rs)
            repo = fixture("RUST-011", "green")
            ci = load_ci_toml(repo).ci
            crates = discover_workspace(root)
            findings = check_rust_011(ci, crates, root)
        return [f.rule for f in findings]

    def test_multiline_cfg_feature(self) -> None:
        findings = self._findings(
            '#[cfg(\n    feature = "json"\n)]\n'
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
        )
        self.assertIn("RUST-011", findings)

    def test_multiline_cfg_attr_feature(self) -> None:
        findings = self._findings(
            '#[cfg_attr(\n    feature = "json",\n    derive(Debug)\n)]\npub struct S;\n'
        )
        self.assertIn("RUST-011", findings)

    def test_multiline_block_comment_mentioning_cfg_feature_stays_green(self) -> None:
        findings = self._findings(
            '/* mentions cfg(\n   feature = "json"\n) in prose, not real code */\n'
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
        )
        self.assertEqual([], findings)

    def test_multiline_raw_string_mentioning_cfg_feature_stays_green(self) -> None:
        findings = self._findings(
            '#[doc = r#"\n'
            '    see cfg(\n    feature = "json"\n    ) for context\n'
            '"#]\n'
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
        )
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
