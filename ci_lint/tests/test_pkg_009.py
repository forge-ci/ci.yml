"""PKG-009 -- ci_lint/rules/pkg_one_compile.py (zackees/ci.yml#118)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.pkg_one_compile import check_pkg_009
from ci_lint.tests.helpers import requires_yaml_tooling

_HEAD = "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
_ROOT = '[workspace]\nmembers = ["core", "py"]\n[workspace.dependencies]\ncore = { path = "core" }\n'
_EXT = (
    '[package]\nname = "core-py"\n[lib]\ncrate-type = ["cdylib"]\n'
    '[dependencies]\npyo3 = "0.22"\ncore = { workspace = true, features = ["a", "b"] }\n'
)
_BASE = {"Cargo.toml": _ROOT, "py/Cargo.toml": _EXT, "core/Cargo.toml": '[package]\nname = "core"\n'}


@requires_yaml_tooling
class Pkg009Test(unittest.TestCase):
    def _run(self, steps: str, extra: dict[str, str] | None = None, base: bool = True) -> list[Finding]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {**(_BASE if base else {}), **(extra or {}), ".github/workflows/ci.yml": _HEAD + steps}
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            return check_pkg_009(root)

    def test_workspace_without_exclude(self) -> None:
        f = self._run("      - run: maturin develop\n      - run: soldr cargo build --workspace\n")
        self.assertEqual([(Status.VIOLATION, "PKG-009")], [(x.status, x.rule) for x in f])

    def test_package_selects_ext(self) -> None:
        f = self._run("      - run: maturin build\n      - run: cargo clippy -p core-py\n")
        self.assertEqual(1, len(f))

    def test_exclude_with_mirrored_features_clean(self) -> None:
        steps = "      - run: maturin build\n      - run: cargo test --workspace --exclude core-py --features core/a,core/b\n"
        self.assertEqual([], self._run(steps))

    def test_exclude_without_mirror(self) -> None:
        steps = "      - run: maturin build\n      - run: cargo test --workspace --exclude core-py --features core/a\n"
        f = self._run(steps)
        self.assertEqual(1, len(f))
        self.assertIn("core/b", f[0].message)

    def test_no_selector_needs_review(self) -> None:
        f = self._run("      - run: maturin build\n      - run: cargo build\n")
        self.assertEqual([Status.NEEDS_REVIEW], [x.status for x in f])

    def test_no_maturin_step_clean(self) -> None:
        self.assertEqual([], self._run("      - run: soldr wheel\n      - run: cargo build --workspace\n"))

    def test_uv_sync_only_with_maturin_backend(self) -> None:
        steps = "      - run: uv sync\n      - run: cargo build --workspace\n"
        self.assertEqual([], self._run(steps))
        backend = {"pyproject.toml": '[build-system]\nbuild-backend = "maturin"\n'}
        self.assertEqual(1, len(self._run(steps, backend)))

    def test_no_extension_crate_clean(self) -> None:
        self.assertEqual([], self._run("      - run: maturin build\n      - run: cargo build --workspace\n", base=False))

    def test_allow_comment(self) -> None:
        steps = "      - run: maturin build\n      - run: cargo build --workspace  # ci-lint: allow PKG-009 measured\n"
        self.assertEqual([], self._run(steps))

    def test_script_one_level(self) -> None:
        script = "import subprocess\nsubprocess.run(['cargo', 'test', '--workspace'], check=True)\n"
        f = self._run("      - run: maturin develop\n      - run: python ci/t.py\n", {"ci/t.py": script})
        self.assertEqual(1, len(f))
        self.assertEqual("ci/t.py", f[0].path)


if __name__ == "__main__":
    unittest.main()
