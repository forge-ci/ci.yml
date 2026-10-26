"""PKG-008 (zackees/ci.yml#119): abi3 cp310 floor -- static Cargo.toml/
pyproject.toml clauses, plus the wheel-check and release-verify runtime
extensions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.release import verify_staged_artifacts
from ci_lint.rules.pkg_abi3 import check_pkg_008
from ci_lint.runtime.wheel import check_wheel
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES
from ci_lint.tests.test_release import CANDIDATE_SHA, _write_sdist
from ci_lint.tests.test_runtime_wheel import PE_X64, _build_wheel

GREEN_PYPROJECT = '[project]\nname = "demo"\nrequires-python = ">=3.10"\n'


class StaticTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _write(self, cargo: str, pyproject: str = GREEN_PYPROJECT) -> list[str]:
        (self.repo / "Cargo.toml").write_text(cargo)
        (self.repo / "pyproject.toml").write_text(pyproject)
        return [f.message for f in check_pkg_008(self.repo)]

    def test_green_workspace_dependency(self) -> None:
        cargo = '[workspace.dependencies]\npyo3 = { version = "0.22", features = ["abi3-py310"] }\n'
        self.assertEqual([], self._write(cargo))

    def test_no_pyo3_is_out_of_scope(self) -> None:
        self.assertEqual([], self._write('[package]\nname = "x"\n', '[project]\nname = "x"\n'))

    def test_missing_abi3_is_violation(self) -> None:
        msgs = self._write('[dependencies]\npyo3 = "0.22"\n')
        self.assertTrue(any("abi3 floor" in m for m in msgs), msgs)

    def test_higher_floor_is_violation(self) -> None:
        msgs = self._write('[dependencies]\npyo3 = { version = "0.22", features = ["abi3-py311"] }\n')
        self.assertTrue(any("abi3-py311" in m for m in msgs), msgs)

    def test_frontend_only_feature_is_violation(self) -> None:
        pyproject = GREEN_PYPROJECT + '[tool.maturin]\nfeatures = ["pyo3/abi3-py310", "pyo3/extension-module"]\n'
        msgs = self._write('[dependencies]\npyo3 = "0.22"\n', pyproject)
        self.assertTrue(any("frontend-side only" in m for m in msgs), msgs)

    def test_requires_python_mismatch(self) -> None:
        cargo = '[dependencies]\npyo3 = { version = "0.22", features = ["abi3-py310"] }\n'
        msgs = self._write(cargo, '[project]\nname = "x"\nrequires-python = ">=3.9"\n')
        self.assertTrue(any("requires-python" in m for m in msgs), msgs)


class WheelCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        loaded = load_ci_toml(FIXTURES / "runtime" / "workspace")
        assert loaded.ci is not None, loaded.findings
        self.ci = loaded.ci
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _pkg008(self, **kw: str) -> list[str]:
        report = check_wheel(self.ci, _build_wheel(self.tmp, **kw))
        return [f.message for f in report.findings if f.rule == "PKG-008"]

    def test_cp310_abi3_passes(self) -> None:
        self.assertEqual([], self._pkg008())

    def test_cp311_abi3_is_pkg_008(self) -> None:
        self.assertTrue(self._pkg008(python_tag="cp311"))

    def test_version_specific_is_pkg_008(self) -> None:
        self.assertTrue(self._pkg008(python_tag="cp311", abi_tag="cp311"))


class ReleaseVerifyTest(unittest.TestCase):
    def test_staged_cpxy_cpxy_wheel_is_pkg_008(self) -> None:
        loaded = load_ci_toml(FIXTURES / "runtime" / "cache" / "repo")
        assert loaded.ci is not None, loaded.findings
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            _build_wheel(dist, platform_tag="manylinux_2_17_x86_64")
            _build_wheel(dist, platform_tag="win_amd64", cli_bytes=PE_X64)
            _write_sdist(dist)
            clean = verify_staged_artifacts(loaded.ci, dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="d")
            self.assertEqual([], [f for f in clean.findings if f.rule == "PKG-008"])
            _build_wheel(dist, python_tag="cp311", abi_tag="cp311", platform_tag="manylinux_2_17_x86_64")
            report = verify_staged_artifacts(loaded.ci, dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="d")
            self.assertTrue(
                any(f.rule == "PKG-008" and "staged release set" in f.message for f in report.findings)
            )


if __name__ == "__main__":
    unittest.main()
