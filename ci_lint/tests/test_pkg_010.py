"""PKG-010 -- ci_lint/rules/wheel_soldr_only.py (zackees/ci.yml#17)."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from ci_lint.rules.wheel_soldr_only import check_pkg_010
from ci_lint.tests.helpers import requires_yaml_tooling


def _repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="pkg010-"))
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _wf(steps: str) -> dict[str, str]:
    body = "on: push\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n" + textwrap.indent(
        textwrap.dedent(steps), "      "
    )
    return {".github/workflows/ci.yml": body}


@requires_yaml_tooling
class Pkg010Test(unittest.TestCase):
    def test_soldr_wheel_is_clean(self) -> None:
        root = _repo(
            _wf("- run: soldr wheel --release --target x86_64-unknown-linux-musl\n")
            | {"pyproject.toml": '[build-system]\nrequires = ["maturin>=1"]\nbuild-backend = "maturin"\n'}
        )
        self.assertEqual([], check_pkg_010(root))

    def test_wrapped_maturin_build_and_develop(self) -> None:
        root = _repo(_wf("- run: uvx maturin build --release\n- run: uv run maturin develop\n"))
        msgs = [f.message for f in check_pkg_010(root)]
        self.assertEqual(2, len(msgs))
        self.assertIn("maturin build", msgs[0])
        self.assertIn("maturin develop", msgs[1])

    def test_bare_maturin_left_to_tool_001(self) -> None:
        root = _repo(_wf("- run: maturin build --release\n- run: cargo zigbuild --release\n"))
        self.assertEqual([], check_pkg_010(root))

    def test_zig_forms(self) -> None:
        root = _repo(
            _wf(
                "- run: uv pip install 'maturin[zig]'\n"
                "- run: uvx maturin build --zig\n"
                "- run: soldr cargo zigbuild --release\n"
                "- run: uv tool install ziglang\n"
                "- run: CC='zig cc' soldr cargo build --locked\n"
            )
        )
        msgs = " ".join(f.message for f in check_pkg_010(root))
        for needle in ("maturin[zig]", "--zig", "cargo zigbuild", "ziglang", "zig cc"):
            self.assertIn(needle, msgs)

    def test_uses_actions(self) -> None:
        root = _repo(_wf("- uses: PyO3/maturin-action@v1\n- uses: mlugg/setup-zig@v2\n- uses: goto-bus-stop/setup-zig@v2\n"))
        self.assertEqual(3, len(check_pkg_010(root)))

    def test_allow_comment(self) -> None:
        root = _repo(_wf("- run: uvx maturin build  # ci-lint: allow PKG-010 tracked in #17\n"))
        self.assertEqual([], check_pkg_010(root))

    def test_ci_script_one_level(self) -> None:
        root = _repo(
            _wf("- run: python ci/build.py\n")
            | {"ci/build.py": 'import subprocess\nsubprocess.run(["uvx", "maturin", "build", "--zig"], check=True)\n'}
        )
        findings = check_pkg_010(root)
        self.assertEqual({"ci/build.py"}, {f.path for f in findings})
        self.assertEqual(2, len(findings))

    def test_pkg_007_job_not_double_reported(self) -> None:
        root = _repo(
            _wf(
                "- run: uvx maturin build --release --zig --target x86_64-unknown-linux-gnu\n"
                "- run: python3 ci/release.py glibc-check --max-glibc 2.17\n"
            )
        )
        msgs = [f.message for f in check_pkg_010(root)]
        self.assertEqual(1, len(msgs))  # maturin build only; --zig is PKG-007's
        self.assertIn("maturin build", msgs[0])

    def test_pyproject_dependency_groups(self) -> None:
        root = _repo(
            {
                "pyproject.toml": '[project]\nname = "x"\n[dependency-groups]\ndev = ["ziglang>=0.11", "pytest"]\n'
                '[tool.uv]\ndev-dependencies = ["maturin[zig]"]\n'
            }
        )
        findings = check_pkg_010(root)
        self.assertEqual(2, len(findings))
        self.assertTrue(all(f.path == "pyproject.toml" for f in findings))


if __name__ == "__main__":
    unittest.main()
