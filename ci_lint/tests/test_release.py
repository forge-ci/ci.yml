"""`ci-lint release verify` -- ci_lint/release.py (round-5 brief, deliverable
3). Reuses test_runtime_wheel.py's synthetic wheel builder (stdlib zipfile
only, no real compiled binaries) rather than duplicating it.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from ci_lint.release import (
    ReleaseVerifyError,
    expected_wheel_tag_pattern,
    verify_readback,
    verify_resume,
    verify_staged_artifacts,
    write_release_manifest,
)
from ci_lint.schema import Platform, load_ci_toml
from ci_lint.tests.helpers import FIXTURES
from ci_lint.tests.test_runtime_wheel import PE_X64, _build_wheel

REPO = FIXTURES / "runtime" / "cache" / "repo"  # linux-x64 + windows-x64, [publish.pypi] set
CANDIDATE_SHA = "a" * 40


def _write_sdist(directory: Path, *, dist: str = "demo_cli", version: str = "0.1.0", backend: str = "soldr") -> Path:
    sdist = directory / f"{dist}-{version}.tar.gz"
    pyproject = directory / "pyproject.toml"
    pyproject.write_text(f'[build-system]\nrequires = ["soldr==1.2.3"]\nbuild-backend = "{backend}"\n', encoding="utf-8")
    with tarfile.open(sdist, "w:gz") as tf:
        tf.add(pyproject, arcname=f"{dist}-{version}/pyproject.toml")
    return sdist


class ExpectedWheelTagPatternTest(unittest.TestCase):
    def test_linux_default_base(self) -> None:
        p = Platform(id="linux-x64", target="x86_64-unknown-linux-gnu", runs_on="ubuntu-24.04", group="linux")
        self.assertRegex("manylinux_2_17_x86_64", expected_wheel_tag_pattern(p))
        self.assertNotRegex("manylinux_2_28_x86_64", expected_wheel_tag_pattern(p))

    def test_linux_arm64_declared_wheel_base(self) -> None:
        p = Platform(
            id="linux-arm64", target="aarch64-unknown-linux-gnu", runs_on="ubuntu-24.04-arm", group="linux",
            wheel="manylinux_2_17",
        )
        self.assertRegex("manylinux_2_17_aarch64", expected_wheel_tag_pattern(p))

    def test_windows_amd64_and_arm64(self) -> None:
        x64 = Platform(id="windows-x64", target="x86_64-pc-windows-msvc", runs_on="windows-2025", group="windows")
        arm = Platform(id="windows-arm64", target="aarch64-pc-windows-msvc", runs_on="windows-11-arm", group="windows")
        self.assertRegex("win_amd64", expected_wheel_tag_pattern(x64))
        self.assertRegex("win_arm64", expected_wheel_tag_pattern(arm))
        self.assertNotRegex("win_arm64", expected_wheel_tag_pattern(x64))

    def test_macos_arm64_and_x86_64_wildcard_version(self) -> None:
        arm = Platform(id="macos-arm64", target="aarch64-apple-darwin", runs_on="macos-15", group="macos")
        x64 = Platform(id="macos-x64", target="x86_64-apple-darwin", runs_on="macos-15-intel", group="macos")
        self.assertRegex("macosx_11_0_arm64", expected_wheel_tag_pattern(arm))
        self.assertRegex("macosx_10_9_x86_64", expected_wheel_tag_pattern(x64))
        self.assertNotRegex("macosx_11_0_x86_64", expected_wheel_tag_pattern(arm))

    def test_unknown_group_returns_none(self) -> None:
        p = Platform(id="embedded", target="thumbv7-none", runs_on="ubuntu-24.04", group="embedded")
        self.assertIsNone(expected_wheel_tag_pattern(p))


class ReleaseVerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dist = Path(self._tmp.name)

    def _stage_green_set(self) -> None:
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        _write_sdist(self.dist)

    def test_green_set_has_no_pkg_006_findings(self) -> None:
        self._stage_green_set()
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        pkg006 = [f for f in report.findings if f.rule == "PKG-006"]
        self.assertEqual([], pkg006, [f.render() for f in pkg006])
        self.assertEqual("0.1.0", report.version)
        self.assertEqual(3, len(report.artifacts))  # 2 wheels + 1 sdist

    def test_compound_platform_tag_matches_any_component(self) -> None:
        # PEP 425: `platform_tag` may be a "."-joined list of tags the
        # wheel is compatible with. maturin emits exactly this for a
        # Linux wheel whose measured glibc floor equals a legacy
        # manylinux1/2010/2014 alias -- e.g. a real, correctly-built
        # manylinux_2_17 wheel is filed as
        # `manylinux_2_17_x86_64.manylinux2014_x86_64` (both describe the
        # identical floor). Before this fix, `_match_platform`'s
        # `pattern.match(...)` against the WHOLE compound string could
        # never match a single `^...$` pattern -- found via a live run,
        # zackees/template-python-rust-cmd#30 round 5.
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64.manylinux2014_x86_64")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        _write_sdist(self.dist)
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        pkg006 = [f for f in report.findings if f.rule == "PKG-006"]
        self.assertEqual([], pkg006, [f.render() for f in pkg006])

    def test_missing_platform_wheel_is_pkg_006(self) -> None:
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64")
        _write_sdist(self.dist)  # windows-x64's wheel never staged
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        self.assertTrue(
            any(f.rule == "PKG-006" and "windows-x64" in f.message and "no staged wheel" in f.message
                for f in report.findings)
        )

    def test_duplicate_platform_wheel_is_pkg_006(self) -> None:
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64")
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64", python_tag="cp311")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        _write_sdist(self.dist)
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        self.assertTrue(
            any(f.rule == "PKG-006" and "linux-x64" in f.message and "2 staged wheels" in f.message
                for f in report.findings)
        )

    def test_extra_unmatched_wheel_is_pkg_006(self) -> None:
        self._stage_green_set()
        _build_wheel(self.dist, dist="demo_cli", platform_tag="musllinux_1_2_x86_64")
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        self.assertTrue(
            any(f.rule == "PKG-006" and "matches no declared" in f.message for f in report.findings)
        )

    def test_version_mismatch_is_pkg_006(self) -> None:
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64", version="0.1.0")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64, version="0.2.0")
        _write_sdist(self.dist, version="0.1.0")
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        self.assertTrue(
            any(f.rule == "PKG-006" and "inconsistent versions" in f.message for f in report.findings)
        )
        self.assertIsNone(report.version)

    def test_missing_sdist_is_pkg_006(self) -> None:
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        self.assertTrue(any(f.rule == "PKG-006" and "no sdist" in f.message for f in report.findings))

    def test_underlying_wheel_check_findings_surface(self) -> None:
        # A wheel with no bundled native extension: PKG-005 from the reused
        # ci_lint.runtime.wheel.check_wheel, not re-implemented here.
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64", include_native=False)
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        _write_sdist(self.dist)
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        self.assertTrue(any(f.rule == "PKG-005" for f in report.findings))

    def test_bad_sha_raises(self) -> None:
        self._stage_green_set()
        with self.assertRaises(ReleaseVerifyError):
            verify_staged_artifacts(self.ci, self.dist, candidate_sha="not-a-sha", ci_toml_digest="deadbeef")

    def test_manifest_written_with_sha256_and_candidate_sha(self) -> None:
        self._stage_green_set()
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef"
        )
        manifest_path = write_release_manifest(report, self.dist)
        self.assertTrue(manifest_path.is_file())
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(CANDIDATE_SHA, payload["candidate_sha"])
        self.assertEqual("deadbeef", payload["ci_toml_digest"])
        self.assertEqual(3, len(payload["artifacts"]))
        for artifact in payload["artifacts"]:
            self.assertEqual(64, len(artifact["sha256"]))

    def test_smoke_missing_result_is_pkg_006(self) -> None:
        self._stage_green_set()
        smoke_dir = self.dist / "smoke-results"
        smoke_dir.mkdir()
        # Only one of the two wheels gets a smoke result.
        linux_wheel = next(self.dist.glob("*manylinux*.whl"))
        (smoke_dir / "linux.json").write_text(
            json.dumps({"platform": "linux-x64", "wheel": linux_wheel.name, "passed": True}), encoding="utf-8"
        )
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef", smoke_dir=smoke_dir
        )
        self.assertTrue(any(f.rule == "PKG-006" and "no smoke result" in f.message for f in report.findings))

    def test_smoke_failing_result_is_pkg_006(self) -> None:
        self._stage_green_set()
        smoke_dir = self.dist / "smoke-results"
        smoke_dir.mkdir()
        for whl in self.dist.glob("*.whl"):
            passed = "win" not in whl.name
            (smoke_dir / f"{whl.stem}.json").write_text(
                json.dumps({"platform": "x", "wheel": whl.name, "passed": passed, "detail": "install failed"}),
                encoding="utf-8",
            )
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef", smoke_dir=smoke_dir
        )
        self.assertTrue(
            any(f.rule == "PKG-006" and "smoke failed" in f.message and "win_amd64" in f.message
                for f in report.findings)
        )

    def test_smoke_all_passing_is_clean(self) -> None:
        self._stage_green_set()
        smoke_dir = self.dist / "smoke-results"
        smoke_dir.mkdir()
        for whl in self.dist.glob("*.whl"):
            (smoke_dir / f"{whl.stem}.json").write_text(
                json.dumps({"platform": "x", "wheel": whl.name, "passed": True}), encoding="utf-8"
            )
        report = verify_staged_artifacts(
            self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef", smoke_dir=smoke_dir
        )
        pkg006 = [f for f in report.findings if f.rule == "PKG-006"]
        self.assertEqual([], pkg006, [f.render() for f in pkg006])


class ReleaseReadbackTest(unittest.TestCase):
    """REL-003 (zackees/ci.yml#8/#74): the dry run must read the mock
    publisher's staged destination back, not just write to it."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dist = Path(self._tmp.name)
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        _write_sdist(self.dist)
        report = verify_staged_artifacts(self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef")
        self.artifacts = report.artifacts

    def test_missing_readback_dir_is_rel_003(self) -> None:
        findings = verify_readback(self.dist, self.dist / "no-such-readback-dir", self.artifacts)
        self.assertTrue(any(f.rule == "REL-003" for f in findings))

    def test_missing_record_is_rel_003(self) -> None:
        readback_dir = self.dist / "readback"
        readback_dir.mkdir()
        findings = verify_readback(self.dist, readback_dir, self.artifacts)
        self.assertTrue(any(f.rule == "REL-003" for f in findings))

    def test_copied_not_read_back_is_rel_003(self) -> None:
        # A record that just echoes the staged sha256 without asserting
        # 'read_back': true proves nothing about the destination's read path.
        readback_dir = self.dist / "readback"
        readback_dir.mkdir()
        for a in self.artifacts:
            (readback_dir / f"{a.path}.json").write_text(
                json.dumps({"path": a.path, "sha256": a.sha256}), encoding="utf-8"
            )
        findings = verify_readback(self.dist, readback_dir, self.artifacts)
        self.assertTrue(any(f.rule == "REL-003" for f in findings))

    def test_mismatched_digest_is_rel_003(self) -> None:
        readback_dir = self.dist / "readback"
        readback_dir.mkdir()
        for a in self.artifacts:
            (readback_dir / f"{a.path}.json").write_text(
                json.dumps({"path": a.path, "sha256": "0" * 64, "read_back": True}), encoding="utf-8"
            )
        findings = verify_readback(self.dist, readback_dir, self.artifacts)
        self.assertTrue(any(f.rule == "REL-003" and "digest" in f.message for f in findings))

    def test_real_readback_is_clean(self) -> None:
        readback_dir = self.dist / "readback"
        readback_dir.mkdir()
        for a in self.artifacts:
            (readback_dir / f"{a.path}.json").write_text(
                json.dumps({"path": a.path, "sha256": a.sha256, "read_back": True}), encoding="utf-8"
            )
        findings = verify_readback(self.dist, readback_dir, self.artifacts)
        self.assertEqual([], findings, [f.render() for f in findings])


class ReleaseResumeTest(unittest.TestCase):
    """REL-004 (zackees/ci.yml#8/#74): a resume must reuse the exact frozen
    bytes a prior release-manifest.json hashed, never rebuild."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dist = Path(self._tmp.name)
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64")
        _build_wheel(self.dist, platform_tag="win_amd64", cli_bytes=PE_X64)
        _write_sdist(self.dist)
        report = verify_staged_artifacts(self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef")
        self.frozen_manifest = write_release_manifest(report, self.dist)

    def test_identical_bytes_resume_is_clean(self) -> None:
        report = verify_staged_artifacts(self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef")
        findings = verify_resume(self.dist, self.frozen_manifest, report.artifacts)
        self.assertEqual([], findings, [f.render() for f in findings])

    def test_rebuilt_bytes_are_rel_004(self) -> None:
        # Re-build the same wheel filename with different content -- simulates
        # a non-reproducible rebuild producing non-identical bytes.
        _build_wheel(self.dist, platform_tag="manylinux_2_17_x86_64", cli_bytes=b"\x7fELF" + b"different")
        report = verify_staged_artifacts(self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef")
        findings = verify_resume(self.dist, self.frozen_manifest, report.artifacts)
        self.assertTrue(any(f.rule == "REL-004" and "rebuilt" in f.message for f in findings))

    def test_missing_frozen_artifact_is_rel_004(self) -> None:
        wheel = next(self.dist.glob("*manylinux*.whl"))
        wheel.unlink()
        report = verify_staged_artifacts(self.ci, self.dist, candidate_sha=CANDIDATE_SHA, ci_toml_digest="deadbeef")
        findings = verify_resume(self.dist, self.frozen_manifest, report.artifacts)
        self.assertTrue(any(f.rule == "REL-004" for f in findings))
