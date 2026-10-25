"""CACHE-039 -- ci_lint/rules/cache_target_settings.py (zackees/ci.yml#148)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.cache_target_settings import check_cache_039
from ci_lint.tests.helpers import requires_yaml_tooling

_HEAD = "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"


@requires_yaml_tooling
class Cache039Test(unittest.TestCase):
    def _run(self, steps: str, files: dict[str, str] | None = None) -> list[Finding]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text(_HEAD + steps, encoding="utf-8")
            for rel, text in (files or {}).items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            return check_cache_039(root)

    def test_same_settings_is_clean(self) -> None:
        steps = "      - run: soldr cargo build --locked\n      - run: soldr cargo test --locked\n"
        self.assertEqual([], self._run(steps))

    def test_different_features_same_dir(self) -> None:
        steps = "      - run: cargo clippy --all-features\n      - run: cargo test --features 'a b'\n"
        findings = self._run(steps)
        self.assertEqual(1, len(findings))
        self.assertEqual((Status.NEEDS_REVIEW, "CACHE-039"), (findings[0].status, findings[0].rule))
        self.assertIn("features", findings[0].message)

    def test_different_rustflags(self) -> None:
        steps = (
            "      - run: cargo build\n"
            "      - env:\n          RUSTFLAGS: -Dwarnings\n        run: cargo test\n"
        )
        findings = self._run(steps)
        self.assertEqual(1, len(findings))
        self.assertIn("RUSTFLAGS=-Dwarnings", findings[0].message)

    def test_separate_target_dirs_and_profiles_are_clean(self) -> None:
        steps = (
            "      - run: cargo build\n"
            "      - run: CARGO_TARGET_DIR=target/x cargo test --all-features\n"
            "      - run: cargo build --release --features x\n"
        )
        self.assertEqual([], self._run(steps))

    def test_allow_comment(self) -> None:
        steps = (
            "      - run: cargo build\n"
            "      - run: cargo test --features x  # ci-lint: allow CACHE-039 disjoint crate\n"
        )
        self.assertEqual([], self._run(steps))

    def test_maturin_vs_cargo_cites_pkg_009(self) -> None:
        steps = "      - run: maturin build --release\n      - run: cargo build --release\n"
        findings = self._run(steps)
        self.assertEqual(1, len(findings))
        self.assertIn("PKG-009", findings[0].message)

    def test_uv_run_only_with_pyo3(self) -> None:
        steps = "      - run: cargo test\n      - run: uv run cargo test\n"
        self.assertEqual([], self._run(steps))
        findings = self._run(steps, {"Cargo.toml": '[dependencies]\npyo3 = "0.22"\n'})
        self.assertEqual(1, len(findings))
        self.assertIn("RUST-030", findings[0].message)

    def test_script_one_level_deep(self) -> None:
        steps = "      - run: cargo build\n      - run: python ci/t.py\n"
        script = "import subprocess\nsubprocess.run(['cargo', 'test', '--no-default-features'], check=True)\n"
        findings = self._run(steps, {"ci/t.py": script})
        self.assertEqual(1, len(findings))
        self.assertEqual(("ci/t.py", 2), (findings[0].path, findings[0].line))

    def test_dylint_left_to_rust_016(self) -> None:
        steps = "      - run: cargo build\n      - run: RUSTFLAGS=-x soldr cargo dylint --all\n"
        self.assertEqual([], self._run(steps))


if __name__ == "__main__":
    unittest.main()
