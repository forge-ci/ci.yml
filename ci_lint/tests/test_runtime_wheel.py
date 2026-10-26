"""`ci-lint wheel check` / `ci-lint wheel installed` -- ci_lint/runtime/wheel.py.

Builds small synthetic wheel .zip files and fake ELF/PE/Mach-O header
bytes in a tempdir rather than shipping real compiled binaries as
fixtures (round-2A brief part 2c: stdlib zipfile only).
"""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.runtime.wheel import (
    check_installed,
    check_magic,
    check_wheel,
    parse_wheel_filename,
    soldr_requires_floor,
)
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES


def _elf_header(machine: int) -> bytes:
    return b"\x7fELF" + b"\x00" * 14 + machine.to_bytes(2, "little") + b"\x00" * 100


def _pe_header(machine: int) -> bytes:
    lfanew = 0x40
    header = bytearray(lfanew + 6 + 100)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = lfanew.to_bytes(4, "little")
    header[lfanew : lfanew + 4] = b"PE\x00\x00"
    header[lfanew + 4 : lfanew + 6] = machine.to_bytes(2, "little")
    return bytes(header)


def _macho_header(cputype: int) -> bytes:
    return b"\xcf\xfa\xed\xfe" + cputype.to_bytes(4, "little") + b"\x00" * 100


ELF_X86_64 = _elf_header(0x3E)
ELF_AARCH64 = _elf_header(0xB7)
PE_X64 = _pe_header(0x8664)
MACHO_ARM64 = _macho_header(0x0100000C)


def _build_wheel(
    directory: Path,
    *,
    dist: str = "demo_cli",
    version: str = "0.1.0",
    python_tag: str = "cp310",
    abi_tag: str = "abi3",
    platform_tag: str = "manylinux_2_17_x86_64",
    cli_name: str = "demo-cli",
    cli_bytes: bytes = ELF_X86_64,
    shebang: bool = False,
    include_script: bool = True,
    include_native: bool = True,
    generator: str = "soldr 1.2.3",
    console_script: str | None = None,
) -> Path:
    import zipfile

    name = f"{dist}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
    whl_path = directory / name
    with zipfile.ZipFile(whl_path, "w") as zf:
        if include_script:
            script_name = cli_name + (".exe" if platform_tag.startswith("win") else "")
            content = (b"#!/bin/sh\n" if shebang else b"") + cli_bytes
            zf.writestr(f"{dist}-{version}.data/scripts/{script_name}", content)
        if include_native:
            zf.writestr(f"{dist}/_native.abi3.so", ELF_X86_64)
        dist_info = f"{dist}-{version}.dist-info/"
        zf.writestr(
            dist_info + "WHEEL",
            f"Wheel-Version: 1.0\nGenerator: {generator}\nRoot-Is-Purelib: false\n"
            f"Tag: {python_tag}-{abi_tag}-{platform_tag}\n",
        )
        ep_text = "[console_scripts]\n"
        if console_script:
            ep_text += f"{console_script} = {dist}:main\n"
        zf.writestr(dist_info + "entry_points.txt", ep_text)
        zf.writestr(dist_info + "METADATA", f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    return whl_path


class MagicCheckUnitTest(unittest.TestCase):
    def test_elf_x86_64_matches(self) -> None:
        ok, detail = check_magic(ELF_X86_64, "elf", "x86_64")
        self.assertTrue(ok, detail)

    def test_elf_wrong_arch_fails(self) -> None:
        ok, detail = check_magic(ELF_X86_64, "elf", "aarch64")
        self.assertFalse(ok)
        self.assertIn("e_machine", detail)

    def test_elf_wrong_magic_fails(self) -> None:
        ok, _ = check_magic(b"not an elf file at all", "elf", "x86_64")
        self.assertFalse(ok)

    def test_pe_x64_matches(self) -> None:
        ok, detail = check_magic(PE_X64, "pe", "x86_64")
        self.assertTrue(ok, detail)

    def test_macho_arm64_matches(self) -> None:
        ok, detail = check_magic(MACHO_ARM64, "macho", "aarch64")
        self.assertTrue(ok, detail)

    def test_macho_wrong_arch_fails(self) -> None:
        ok, _ = check_magic(MACHO_ARM64, "macho", "x86_64")
        self.assertFalse(ok)


class WheelFilenameParseTest(unittest.TestCase):
    def test_five_component_filename(self) -> None:
        parts = parse_wheel_filename(Path("demo_cli-0.1.0-cp310-abi3-manylinux_2_17_x86_64.whl"))
        assert parts is not None
        self.assertEqual("demo_cli", parts.distribution)
        self.assertEqual("abi3", parts.abi_tag)
        self.assertIsNone(parts.build_tag)

    def test_six_component_filename_with_build_tag(self) -> None:
        parts = parse_wheel_filename(Path("demo_cli-0.1.0-2-cp310-abi3-manylinux_2_17_x86_64.whl"))
        assert parts is not None
        self.assertEqual("2", parts.build_tag)

    def test_malformed_filename_returns_none(self) -> None:
        self.assertIsNone(parse_wheel_filename(Path("not-a-wheel.txt")))


class WheelCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FIXTURES / "runtime" / "workspace"
        _loaded = load_ci_toml(self.repo)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        # This fixture's [python].cli.name/crate is "demo-cli" and
        # [python].abi3 is "cp310" -- reuse it rather than declaring a
        # third near-duplicate ci.toml just for the wheel checks.
        self.ci = ci
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def test_green_wheel_has_no_violations(self) -> None:
        whl = _build_wheel(self.tmp_path)
        report = check_wheel(self.ci, whl)
        violations = [f for f in report.findings if f.status.value == "violation"]
        self.assertEqual([], violations, msg=[f.render() for f in violations])
        self.assertEqual("soldr 1.2.3", report.generator)

    def test_missing_script_entry_is_pkg_003(self) -> None:
        whl = _build_wheel(self.tmp_path, include_script=False)
        report = check_wheel(self.ci, whl)
        self.assertTrue(any(f.rule == "PKG-003" and "data/scripts" in f.message for f in report.findings))

    def test_shebang_on_bundled_cli_is_pkg_003(self) -> None:
        whl = _build_wheel(self.tmp_path, shebang=True)
        report = check_wheel(self.ci, whl)
        self.assertTrue(any(f.rule == "PKG-003" and "shebang" in f.message for f in report.findings))

    def test_wrong_arch_magic_bytes_is_pkg_003(self) -> None:
        whl = _build_wheel(self.tmp_path, cli_bytes=ELF_AARCH64)  # tag says x86_64
        report = check_wheel(self.ci, whl)
        self.assertTrue(any(f.rule == "PKG-003" and "magic bytes" in f.message for f in report.findings))

    def test_console_scripts_shadow_is_pkg_003(self) -> None:
        whl = _build_wheel(self.tmp_path, console_script="demo-cli")
        report = check_wheel(self.ci, whl)
        self.assertTrue(any(f.rule == "PKG-003" and "console_scripts" in f.message for f in report.findings))

    def _generator_findings(self, generator: str, floor_fixture: str | None) -> list[Finding]:
        whl = _build_wheel(self.tmp_path, generator=generator)
        floor = soldr_requires_floor(FIXTURES / "PKG-004" / floor_fixture) if floor_fixture else None
        report = check_wheel(self.ci, whl, soldr_floor=floor)
        return [f for f in report.findings if f.rule == "PKG-004" and "Generator" in f.message]

    def test_generator_without_known_floor_is_needs_review(self) -> None:
        pkg004 = self._generator_findings("bdist_wheel (0.42.0)", None)
        self.assertEqual(1, len(pkg004))
        self.assertEqual("needs_review", pkg004[0].status.value)

    def test_generator_below_stamp_floor_is_needs_review(self) -> None:
        pkg004 = self._generator_findings("maturin (1.14.1)", "needs-review-pre-stamp-floor")
        self.assertEqual(1, len(pkg004))
        self.assertEqual("needs_review", pkg004[0].status.value)
        self.assertIn("soldr>=0.9.26", pkg004[0].fix)

    def test_red_unstamped_generator_at_stamp_floor_is_violation(self) -> None:
        # ci.yml#71: soldr >= 0.9.26 stamps its Generator, so a bare maturin
        # Generator under that floor means soldr was bypassed.
        pkg004 = self._generator_findings("maturin (1.14.1)", "red-generator-floor")
        self.assertEqual(1, len(pkg004))
        self.assertEqual("violation", pkg004[0].status.value)

    def test_green_soldr_stamped_generator_at_stamp_floor_passes(self) -> None:
        pkg004 = self._generator_findings("soldr 0.9.26 (maturin (1.14.1-post.1))", "green-generator-floor")
        self.assertEqual([], pkg004)

    def test_soldr_requires_floor_parses_operators(self) -> None:
        self.assertEqual((0, 9, 26), soldr_requires_floor(FIXTURES / "PKG-004" / "red-generator-floor"))
        self.assertEqual((1, 2, 3), soldr_requires_floor(FIXTURES / "PKG-004" / "green"))
        self.assertIsNone(soldr_requires_floor(self.tmp_path))

    def test_wrong_abi_tag_is_pkg_004(self) -> None:
        whl = _build_wheel(self.tmp_path, abi_tag="cp310")
        report = check_wheel(self.ci, whl)
        self.assertTrue(any(f.rule == "PKG-004" and "abi tag" in f.message for f in report.findings))

    def test_missing_native_extension_is_pkg_005(self) -> None:
        whl = _build_wheel(self.tmp_path, include_native=False)
        report = check_wheel(self.ci, whl)
        self.assertTrue(any(f.rule == "PKG-005" for f in report.findings))

    def test_sdist_with_correct_backend_is_clean(self) -> None:
        whl = _build_wheel(self.tmp_path)
        sdist = self.tmp_path / "demo_cli-0.1.0.tar.gz"
        pyproject = self.tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[build-system]\nrequires = ["soldr==1.2.3"]\nbuild-backend = "soldr"\n', encoding="utf-8"
        )
        with tarfile.open(sdist, "w:gz") as tf:
            tf.add(pyproject, arcname="demo_cli-0.1.0/pyproject.toml")
        report = check_wheel(self.ci, whl, sdist_path=sdist)
        self.assertEqual([], [f for f in report.findings if f.path and "tar.gz" in f.path])

    def test_sdist_with_wrong_backend_is_pkg_004(self) -> None:
        whl = _build_wheel(self.tmp_path)
        sdist = self.tmp_path / "demo_cli-0.1.0.tar.gz"
        pyproject = self.tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        with tarfile.open(sdist, "w:gz") as tf:
            tf.add(pyproject, arcname="demo_cli-0.1.0/pyproject.toml")
        report = check_wheel(self.ci, whl, sdist_path=sdist)
        self.assertTrue(any(f.rule == "PKG-004" and "setuptools" in f.message for f in report.findings))


@unittest.skipUnless(os.name == "posix", "check_installed executes the CLI; this test uses a POSIX shebang script")
class WheelInstalledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FIXTURES / "runtime" / "workspace"
        _loaded = load_ci_toml(self.repo)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.venv = Path(self._tmp.name) / "venv"
        (self.venv / "bin").mkdir(parents=True)

    def _write_executable(self, path: Path, content: bytes) -> None:
        path.write_bytes(content)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def test_missing_cli_is_pkg_003(self) -> None:
        report = check_installed(self.ci, self.repo, self.venv)
        self.assertTrue(any(f.rule == "PKG-003" and "no 'demo-cli" in f.message for f in report.findings))

    def test_cli_version_success_and_wrong_magic_reported(self) -> None:
        cli = self.venv / "bin" / "demo-cli"
        self._write_executable(cli, b"#!/bin/sh\necho demo-cli 0.1.0\nexit 0\n")
        report = check_installed(self.ci, self.repo, self.venv)
        self.assertEqual(str(cli), report.cli_path)
        # A shell script is never a real ELF/PE/Mach-O binary, so the
        # magic-byte check must flag it -- while --version still succeeds.
        magic_findings = [f for f in report.findings if "magic bytes" in f.message]
        version_findings = [f for f in report.findings if "--version" in f.message]
        self.assertTrue(magic_findings)
        self.assertEqual([], version_findings)

    def test_cli_version_nonzero_exit_is_pkg_003(self) -> None:
        cli = self.venv / "bin" / "demo-cli"
        self._write_executable(cli, b"#!/bin/sh\nexit 7\n")
        report = check_installed(self.ci, self.repo, self.venv)
        self.assertTrue(any("--version" in f.message for f in report.findings))


if __name__ == "__main__":
    unittest.main()
