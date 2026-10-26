"""LAYOUT-001 -- ci_lint/rules/layout.py. Pure filesystem + regex; no YAML
tooling needed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.rules.layout import check_group6
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, init_git_repo


class LayoutFixtureTest(unittest.TestCase):
    def test_layout_001_host_selector_outside_facade(self) -> None:
        repo = fixture("LAYOUT-001", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("LAYOUT-001", [f.rule for f in check_group6(ci, repo)])

        repo = fixture("LAYOUT-001", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("LAYOUT-001", [f.rule for f in check_group6(ci, repo)])

    def test_layout_001_ignores_gitignored_vendored_code(self) -> None:
        """Regression for round-2A defect 1: a gitignored
        `.cargo/registry/**` vendored dependency must never be scanned.
        Round-1A's filesystem-walk scan had no `.cargo` exclusion at all, so
        a real checkout's `.cargo/registry/**` produced 1,750 bogus
        LAYOUT-001 findings against the template repo. Built as a throwaway
        git repo (not a static checked-in fixture) because a genuinely
        gitignored file can never be committed to ci.yml's own repository
        without defeating the point of the test."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / ".gitignore").write_text("target/\n.cargo/registry/\n", encoding="utf-8")
            vendored = root / ".cargo" / "registry" / "src" / "bogus-dep-1.0.0"
            vendored.mkdir(parents=True)
            (vendored / "lib.rs").write_text(
                "#[cfg(windows)]\nfn vendored_windows_only() {}\n", encoding="utf-8"
            )
            repo = fixture("LAYOUT-001", "green")
            ci = load_ci_toml(repo).ci
            findings = check_group6(ci, root)
        self.assertEqual([], findings)

    def test_layout_001_comment_mentioning_a_selector_is_not_a_violation(self) -> None:
        """Regression for round-2A defect 2: a doc comment merely
        mentioning `#[cfg(target_os = ...)]` must not be flagged as though
        it were real code outside the platform facade."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crates" / "demo" / "src").mkdir(parents=True)
            (root / "crates" / "demo" / "src" / "lib.rs").write_text(
                '//! supports #[cfg(target_os = "linux")] per platform\n'
                "/// see also cfg(feature = \"json\")\n"
                "fn real() {}\n",
                encoding="utf-8",
            )
            repo = fixture("LAYOUT-001", "green")
            ci = load_ci_toml(repo).ci

            # Reuse the green fixture's ci.toml (a valid, empty allowlist
            # match for this new path) against the temp-dir source tree.
            findings = check_group6(ci, root)
        self.assertEqual([], findings)

    def test_layout_001_python_comment_mentioning_sys_platform_is_not_a_violation(self) -> None:
        """Round-2A brief, defect 2: a `# ... sys.platform ...` Python
        comment must not be flagged as a real `sys.platform` use."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True)
            (root / "src" / "app.py").write_text(
                "# sys.platform checks live in platforms/, not here\n"
                "def real():\n"
                "    return 1\n",
                encoding="utf-8",
            )
            repo = fixture("LAYOUT-001", "green")
            ci = load_ci_toml(repo).ci
            findings = check_group6(ci, root)
        self.assertEqual([], findings)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class LayoutMultilineAttributeTest(unittest.TestCase):
    """Round-6B regression (found on zackees/template-python-rust-cmd,
    round 2F): LAYOUT-001's Rust scan matched `#[cfg(...)]` / `cfg!(...)`
    / `cfg_select! {...}` only when the whole form sat on one source
    line, so splitting it across lines evaded the cheap precheck scan
    entirely (Dylint's `platform_boundary` lint still caught it in the
    template -- defense in depth -- but the always-on precheck must too;
    see zackees/soldr#3284 for why). Each case reuses the LAYOUT-001
    green fixture's `ci.toml` against a throwaway source tree, the same
    pattern the round-2A regression tests above use."""

    def _findings(self, content: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "crates/demo/src/other.rs", content)
            repo = fixture("LAYOUT-001", "green")
            ci = load_ci_toml(repo).ci
            findings = check_group6(ci, root)
        return [f.rule for f in findings]

    def test_exact_defect_snippet_from_round_2f(self) -> None:
        """The literal snippet from the round-2F defect report."""
        findings = self._findings("#[cfg(\n    windows\n)]\nfn f() {}\n")
        self.assertIn("LAYOUT-001", findings)

    def test_multiline_cfg_any(self) -> None:
        findings = self._findings(
            '#[cfg(any(\n    windows,\n    target_os = "macos"\n))]\nfn f() {}\n'
        )
        self.assertIn("LAYOUT-001", findings)

    def test_multiline_cfg_attr(self) -> None:
        findings = self._findings("#[cfg_attr(\n    unix,\n    derive(Debug)\n)]\nstruct S;\n")
        self.assertIn("LAYOUT-001", findings)

    def test_multiline_cfg_macro(self) -> None:
        findings = self._findings("fn f() -> bool {\n    cfg!(\n        windows\n    )\n}\n")
        self.assertIn("LAYOUT-001", findings)

    def test_multiline_cfg_select(self) -> None:
        findings = self._findings(
            "cfg_select! {\n"
            "    unix => {\n"
            "        fn f() -> u8 { 1 }\n"
            "    },\n"
            "    windows => {\n"
            "        fn f() -> u8 { 2 }\n"
            "    },\n"
            "}\n"
        )
        self.assertIn("LAYOUT-001", findings)

    def test_multiline_cfg_feature_is_not_a_host_selector(self) -> None:
        """`feature` is not in RUST_SELECTORS; a multi-line
        `#[cfg(feature = ...)]` is RUST-011's concern, not LAYOUT-001's,
        and must stay green here."""
        findings = self._findings('#[cfg(\n    feature = "json"\n)]\npub fn add() {}\n')
        self.assertEqual([], findings)

    def test_multiline_block_comment_mentioning_selector(self) -> None:
        findings = self._findings(
            '/* platform selection:\n   #[cfg(target_os = "linux")]\n*/\nfn f() {}\n'
        )
        self.assertEqual([], findings)

    def test_multiline_raw_string_mentioning_selector(self) -> None:
        """A raw string spanning multiple lines, embedded as an attribute
        value, whose content merely mentions `#[cfg(...)]` and even
        contains literal `[`/`]`/`(`/`)` characters must not confuse
        bracket matching or be treated as real code."""
        findings = self._findings(
            '#[doc = r#"\n'
            '    supports #[cfg(target_os = "linux")] per platform\n'
            '"#]\n'
            "fn real() {}\n"
        )
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()


class LayoutGuestPredicateTest(unittest.TestCase):
    """#95: selecting the wasm guest build (`target_family = "wasm"`,
    `target_arch = "wasm32"`, `target_os = "wasi"`) is not host OS
    selection -- kernal-api's `src/guest.rs` false positive."""

    _findings = LayoutMultilineAttributeTest._findings

    def test_wasm_guest_cfg_select_is_not_a_host_selector(self) -> None:
        findings = self._findings(
            "std::cfg_select! {\n"
            '    all(target_family = "wasm", feature = "x") => {\n'
            '        #[path = "guest_x.rs"]\n'
            "        mod x;\n"
            "    }\n"
            "    _ => { use bindings::X; }\n"
            "}\n"
        )
        self.assertEqual([], findings)

    def test_wasm32_and_wasi_attrs_are_not_host_selectors(self) -> None:
        findings = self._findings(
            '#[cfg(target_arch = "wasm32")]\nfn a() {}\n#[cfg(target_os = "wasi")]\nfn b() {}\n'
        )
        self.assertEqual([], findings)

    def test_wasm_mixed_with_host_selector_still_flagged(self) -> None:
        for snippet in (
            '#[cfg(any(target_family = "wasm", windows))]\nfn a() {}\n',
            '#[cfg(all(target_arch = "wasm32", target_os = "linux"))]\nfn a() {}\n',
            '#[cfg(target_family = "unix")]\nfn a() {}\n',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn("LAYOUT-001", self._findings(snippet))
