"""`ci-lint wheel check` and `ci-lint wheel installed` (round-2A brief,
part 2c): prove from the built artifact's own bytes -- never from a build
log or a "trust me" -- that the wheel bundles the native CLI, that CLI's
magic bytes match the wheel's own platform tag, the wheel was produced by
Soldr, its abi tag matches `[python].abi3`, and the `_native` extension is
really in there. stdlib `zipfile`/`tarfile`/`subprocess` only.
"""

from __future__ import annotations

import platform as host_platform
import re
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status
from ci_lint.proc import run_captured
from ci_lint.schema import CiToml

_SCRIPT_MEMBER_RE = re.compile(r"^([^/]+)\.data/scripts/(.+)$")
_NATIVE_MEMBER_RE = re.compile(r"(^|/)_native[^/]*\.(so|pyd|dylib)$")

_ELF_MACHINE: dict[str, int] = {"x86_64": 0x3E, "aarch64": 0xB7}
_PE_MACHINE: dict[str, int] = {"x86_64": 0x8664, "aarch64": 0xAA64}
_MACHO_CPUTYPE: dict[str, int] = {"x86_64": 0x01000007, "aarch64": 0x0100000C}


@dataclass(frozen=True)
class BinaryTarget:
    """An executable format (`"elf" | "pe" | "macho"`) and CPU arch."""

    family: str
    arch: str


@dataclass(frozen=True)
class MagicCheck:
    ok: bool
    detail: str


@dataclass(frozen=True, order=True)
class SoldrVersion:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class WheelFilenameParts:
    distribution: str
    version: str
    build_tag: str | None
    python_tag: str
    abi_tag: str
    platform_tag: str


def parse_wheel_filename(path: Path) -> WheelFilenameParts | None:
    name = path.name
    if not name.endswith(".whl"):
        return None
    stem = name[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) == 5:
        distribution, version, python_tag, abi_tag, platform_tag = parts
        build_tag = None
    elif len(parts) == 6:
        distribution, version, build_tag, python_tag, abi_tag, platform_tag = parts
    else:
        return None
    return WheelFilenameParts(
        distribution=distribution,
        version=version,
        build_tag=build_tag,
        python_tag=python_tag,
        abi_tag=abi_tag,
        platform_tag=platform_tag,
    )


def _platform_family_and_arch(platform_tag: str) -> BinaryTarget | None:
    """`(family, arch)` for a wheel platform tag this round's brief lists
    checks for; `family` is `"elf" | "pe" | "macho"`. Returns None for a
    tag ci-lint does not yet know how to verify (e.g. a pure-Python
    `"any"` wheel) -- callers must treat that as `needs_review`, not a
    pass."""

    if platform_tag.startswith(("manylinux", "musllinux")):
        family = "elf"
    elif platform_tag.startswith("win_"):
        family = "pe"
    elif platform_tag.startswith("macosx"):
        family = "macho"
    else:
        return None
    if platform_tag.endswith("x86_64") or platform_tag == "win_amd64":
        arch = "x86_64"
    elif platform_tag.endswith(("arm64", "aarch64")):
        arch = "aarch64"
    else:
        return None
    return BinaryTarget(family, arch)


def _host_family_and_arch() -> BinaryTarget | None:
    """The family/arch of the machine `ci-lint wheel installed` itself
    runs on -- there is no wheel filename to read a platform tag from once
    a wheel is already installed into a venv, so the host's own
    `sys.platform`/`platform.machine()` stand in for it."""

    machine = host_platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        return None
    if sys.platform.startswith("linux"):
        return BinaryTarget("elf", arch)
    if sys.platform.startswith("win"):
        return BinaryTarget("pe", arch)
    if sys.platform == "darwin":
        return BinaryTarget("macho", arch)
    return None


def check_magic(data: bytes, family: str, arch: str) -> MagicCheck:  # noqa: C901
    """Verify `data` (a binary's leading bytes) is really a `family`
    executable for `arch`, reading only the fixed-offset header fields
    every format guarantees: ELF's `e_machine` (offset 18), PE's
    `e_lfanew`-relative `Machine` field, and Mach-O 64's `cputype`
    (offset 4). Assumes little-endian ELF/Mach-O, true for every target
    x86_64/aarch64 triple this profile ships."""

    if family == "elf":
        if data[:4] != b"\x7fELF":
            return MagicCheck(False, "not an ELF file (missing \\x7fELF magic)")
        if len(data) < 20:
            return MagicCheck(False, "file too short to read e_machine")
        e_machine = int.from_bytes(data[18:20], "little")
        expected = _ELF_MACHINE[arch]
        if e_machine != expected:
            return MagicCheck(False, f"ELF e_machine=0x{e_machine:x}, expected 0x{expected:x} ({arch})")
        return MagicCheck(True, "ok")
    if family == "pe":
        if data[:2] != b"MZ":
            return MagicCheck(False, "not a PE file (missing MZ magic)")
        if len(data) < 0x40:
            return MagicCheck(False, "file too short to read e_lfanew")
        e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
        if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
            return MagicCheck(False, "missing PE\\0\\0 signature at e_lfanew")
        if len(data) < e_lfanew + 6:
            return MagicCheck(False, "file too short to read the PE Machine field")
        machine = int.from_bytes(data[e_lfanew + 4 : e_lfanew + 6], "little")
        expected = _PE_MACHINE[arch]
        if machine != expected:
            return MagicCheck(False, f"PE Machine=0x{machine:x}, expected 0x{expected:x} ({arch})")
        return MagicCheck(True, "ok")
    if family == "macho":
        if data[:4] != b"\xcf\xfa\xed\xfe":
            return MagicCheck(False, "not a 64-bit little-endian Mach-O file (missing magic)")
        if len(data) < 8:
            return MagicCheck(False, "file too short to read cputype")
        cputype = int.from_bytes(data[4:8], "little")
        expected = _MACHO_CPUTYPE[arch]
        if cputype != expected:
            return MagicCheck(False, f"Mach-O cputype=0x{cputype:x}, expected 0x{expected:x} ({arch})")
        return MagicCheck(True, "ok")
    return MagicCheck(False, f"unrecognized family '{family}'")


def _has_console_script(entry_points_text: str, cli_name: str) -> bool:
    section: str | None = None
    for raw_line in entry_points_text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "console_scripts" and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key == cli_name:
                return True
    return False


# zackees/ci.yml#71: soldr stamps `Generator: soldr <version> (maturin ...)`
# into `*.dist-info/WHEEL` since 0.9.26 (zackees/soldr#3450, shipped in
# v0.9.26). A repository whose `[build-system].requires` soldr floor is at or
# above this version can only have produced an unstamped wheel by bypassing
# soldr, so a missing stamp is a hard PKG-004 violation there; below it (or
# with no readable floor) the stamp may legitimately be absent -> needs_review.
GENERATOR_STAMP_MIN_SOLDR = SoldrVersion(0, 9, 26)

_SOLDR_FLOOR_RE = re.compile(r"^soldr\s*(?:==|>=|~=|>)\s*(\d+)\.(\d+)(?:\.(\d+))?")


def soldr_requires_floor(repo_root: Path) -> SoldrVersion | None:
    """The soldr version floor declared in `repo_root/pyproject.toml`'s
    `[build-system].requires` (`soldr>=X.Y.Z`, `==`, `~=`, `>`), or None
    when absent/unreadable."""
    try:
        with (repo_root / "pyproject.toml").open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    build_system = doc.get("build-system")
    requires = build_system.get("requires") if isinstance(build_system, dict) else None
    if not isinstance(requires, list):
        return None
    for req in requires:
        if not isinstance(req, str):
            continue
        m = _SOLDR_FLOOR_RE.match(req.strip())
        if m:
            return SoldrVersion(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    return None


def _parse_wheel_generator(wheel_metadata_text: str) -> str | None:
    for line in wheel_metadata_text.splitlines():
        if line.lower().startswith("generator:"):
            return line.split(":", 1)[1].strip()
    return None


@dataclass(frozen=True)
class WheelCheckReport:
    wheel_path: str
    findings: tuple[Finding, ...]
    generator: str | None
    script_member: str | None
    native_member: str | None


def _check_sdist_backend(sdist_path: Path) -> list[Finding]:
    try:
        with tarfile.open(sdist_path, "r:*") as tf:
            member = next((m for m in tf.getmembers() if m.name.endswith("pyproject.toml")), None)
            if member is None:
                return [
                    Finding(
                        rule="PKG-004",
                        path=sdist_path.name,
                        message="sdist has no pyproject.toml",
                        fix="rebuild the sdist; it must carry pyproject.toml (PEP 517/518)",
                    )
                ]
            fh = tf.extractfile(member)
            if fh is None:
                return []
            doc = tomllib.load(fh)
    except (OSError, tarfile.TarError, tomllib.TOMLDecodeError) as exc:
        return [
            Finding(
                rule="PKG-004",
                status=Status.NEEDS_REVIEW,
                path=sdist_path.name,
                message=f"cannot read sdist: {exc}",
                fix="ensure --sdist points at a valid .tar.gz produced by 'uv build --sdist'",
            )
        ]
    build_system = doc.get("build-system")
    backend = build_system.get("build-backend") if isinstance(build_system, dict) else None
    if backend != "soldr":
        return [
            Finding(
                rule="PKG-004",
                path=sdist_path.name,
                message=f"sdist's pyproject.toml build-backend = {backend!r}, expected 'soldr'",
                fix="rebuild the sdist from a pyproject.toml with "
                '[build-system] build-backend = "soldr"',
            )
        ]
    return []


def check_wheel(  # noqa: C901
    ci: CiToml,
    wheel_path: Path,
    sdist_path: Path | None = None,
    *,
    soldr_floor: SoldrVersion | None = None,
) -> WheelCheckReport:
    """`soldr_floor` is the repository's `[build-system].requires` soldr
    floor (`soldr_requires_floor`); at or above `GENERATOR_STAMP_MIN_SOLDR`
    a WHEEL Generator that does not name soldr is a PKG-004 violation,
    otherwise it is needs_review (ci.yml#71)."""
    findings: list[Finding] = []
    cli_name = ci.python.cli.name if ci.python is not None else ""

    parts = parse_wheel_filename(wheel_path)
    if parts is None:
        findings.append(
            Finding(
                rule="PKG-003",
                path=wheel_path.name,
                message=f"'{wheel_path.name}' is not a well-formed wheel filename (PEP 427: "
                "{distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl)",
                fix="rebuild with 'soldr wheel', which names wheels per PEP 427",
            )
        )
        return WheelCheckReport(
            wheel_path=str(wheel_path), findings=tuple(findings), generator=None, script_member=None, native_member=None
        )

    generator: str | None = None
    script_member: str | None = None
    native_member: str | None = None

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()

        script_entries: dict[str, str] = {}
        for n in names:
            m = _SCRIPT_MEMBER_RE.match(n)
            if m:
                script_entries[m.group(2)] = n
        candidates = [cli_name, f"{cli_name}.exe"]
        script_member = next((script_entries[c] for c in candidates if c in script_entries), None)
        if script_member is None:
            findings.append(
                Finding(
                    rule="PKG-003",
                    path=wheel_path.name,
                    message=f"no '<dist>.data/scripts/{cli_name}[.exe]' entry in the wheel "
                    f"(scripts present: {sorted(script_entries) or 'none'})",
                    fix="bundle the compiled CLI binary into <dist>.data/scripts/ (soldr's PEP 517 "
                    "backend does this from [tool.soldr.pep517].bundle-bins) rather than adding a "
                    "console_scripts shim",
                )
            )
        else:
            data = zf.read(script_member)
            if data.startswith(b"#!"):
                findings.append(
                    Finding(
                        rule="PKG-003",
                        path=f"{wheel_path.name}:{script_member}",
                        message="the bundled CLI entry has a '#!' shebang -- it must be the compiled "
                        "native binary, not a script wrapper",
                        fix="bundle the compiled Rust binary itself; a shebang means a Python or shell "
                        "shim slipped in instead",
                    )
                )
            target = _platform_family_and_arch(parts.platform_tag)
            if target is None:
                findings.append(
                    Finding(
                        rule="PKG-003",
                        status=Status.NEEDS_REVIEW,
                        path=f"{wheel_path.name}:{script_member}",
                        message=f"unrecognized wheel platform tag '{parts.platform_tag}'; cannot verify "
                        "the bundled binary's architecture from its magic bytes",
                        fix="use a platform tag ci-lint recognizes (manylinux*/musllinux*/win_amd64/"
                        "win_arm64/macosx_*_x86_64/macosx_*_arm64), or extend "
                        "ci_lint.runtime.wheel's platform-tag table",
                    )
                )
            else:
                magic = check_magic(data, target.family, target.arch)
                ok, detail = magic.ok, magic.detail
                if not ok:
                    findings.append(
                        Finding(
                            rule="PKG-003",
                            path=f"{wheel_path.name}:{script_member}",
                            message=f"bundled CLI's magic bytes don't match wheel platform tag "
                            f"'{parts.platform_tag}': {detail}",
                            fix=f"rebuild the wheel for '{parts.platform_tag}' so its bundled binary is "
                            f"a real {target.arch} {target.family} executable, not a mismatched or stub binary",
                        )
                    )

        entry_points_member = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
        if entry_points_member is not None:
            text = zf.read(entry_points_member).decode("utf-8", errors="replace")
            if _has_console_script(text, cli_name):
                findings.append(
                    Finding(
                        rule="PKG-003",
                        path=f"{wheel_path.name}:{entry_points_member}",
                        message=f"entry_points.txt declares a console_scripts entry named "
                        f"'{cli_name}', shadowing the native binary bundled at .data/scripts/",
                        fix="remove the console_scripts entry from pyproject.toml's "
                        "[project.scripts]/[project.gui-scripts] (PKG-003); the command on PATH must "
                        "be the bundled native binary alone",
                    )
                )

        wheel_meta_member = next((n for n in names if n.endswith(".dist-info/WHEEL")), None)
        if wheel_meta_member is None:
            findings.append(
                Finding(
                    rule="PKG-004",
                    path=wheel_path.name,
                    message="no '*.dist-info/WHEEL' metadata file in the wheel",
                    fix="rebuild the wheel; every wheel must carry a WHEEL metadata file (PEP 427)",
                )
            )
        else:
            wheel_text = zf.read(wheel_meta_member).decode("utf-8", errors="replace")
            generator = _parse_wheel_generator(wheel_text)
            if generator is None or "soldr" not in generator.lower():
                floor_text = str(GENERATOR_STAMP_MIN_SOLDR)
                if soldr_floor is not None and soldr_floor >= GENERATOR_STAMP_MIN_SOLDR:
                    findings.append(
                        Finding(
                            rule="PKG-004",
                            path=f"{wheel_path.name}:{wheel_meta_member}",
                            message=f"WHEEL Generator is {generator!r}, which does not mention soldr, "
                            f"but pyproject.toml requires soldr >= {floor_text} (which stamps "
                            "'Generator: soldr <version> (...)' on every wheel it builds)",
                            fix="build the wheel through soldr (the soldr PEP 517 backend via 'uv build', "
                            "or 'soldr wheel'), never bare maturin/pip wheel; a soldr-built wheel "
                            "carries the stamp automatically",
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            rule="PKG-004",
                            status=Status.NEEDS_REVIEW,
                            path=f"{wheel_path.name}:{wheel_meta_member}",
                            message=f"WHEEL Generator is {generator!r}, which does not mention soldr",
                            fix=f"raise pyproject.toml's [build-system].requires floor to "
                            f"'soldr>={floor_text}' (the first soldr that stamps its Generator), "
                            "rebuild, and confirm the wheel came from soldr's PEP 517 backend",
                        )
                    )

        if ci.python is not None and ci.python.abi3:
            if parts.abi_tag != "abi3":
                findings.append(
                    Finding(
                        rule="PKG-004",
                        path=wheel_path.name,
                        message=f"wheel abi tag is '{parts.abi_tag}', expected 'abi3' "
                        f"([python].abi3 = {ci.python.abi3!r})",
                        fix="build with soldr's abi3 wheel mode so the wheel is tagged 'abi3', or "
                        "update [python].abi3 in ci.toml to match the actual build",
                    )
                )
            elif parts.python_tag != ci.python.abi3:
                findings.append(
                    Finding(
                        rule="PKG-004",
                        path=wheel_path.name,
                        message=f"wheel python tag is '{parts.python_tag}', expected "
                        f"'{ci.python.abi3}' ([python].abi3)",
                        fix=f"build for the abi3 floor '{ci.python.abi3}' declared in ci.toml's "
                        "[python].abi3, or update ci.toml to match the actual floor this wheel targets",
                    )
                )

        if ci.profile == "rust-pypi-app" and (parts.abi_tag != "abi3" or parts.python_tag != "cp310"):
            findings.append(
                Finding(
                    rule="PKG-008",
                    path=wheel_path.name,
                    message=f"wheel tag is '{parts.python_tag}-{parts.abi_tag}', expected 'cp310-abi3' "
                    "(the fleet abi3 floor, zackees/ci.yml#119)",
                    fix='enable pyo3\'s "abi3-py310" feature on the Cargo dependency and rebuild with '
                    'soldr wheel (a higher floor needs an [[exceptions]] entry, rule = "PKG-008")',
                )
            )

        native_member = next((n for n in names if _NATIVE_MEMBER_RE.search(n)), None)
        if native_member is None:
            findings.append(
                Finding(
                    rule="PKG-005",
                    path=wheel_path.name,
                    message="no '_native' compiled extension file (*.so/*.pyd/*.dylib) found in the wheel",
                    fix="ensure the PyO3 extension module '_native' is bundled into the wheel by the "
                    "soldr backend; a missing extension must fail the build, not silently ship a "
                    "pure-Python fallback",
                )
            )

    if sdist_path is not None:
        findings.extend(_check_sdist_backend(sdist_path))

    return WheelCheckReport(
        wheel_path=str(wheel_path),
        findings=tuple(findings),
        generator=generator,
        script_member=script_member,
        native_member=native_member,
    )


def _python_package_name(repo_root: Path) -> str | None:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = doc.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name:
        return None
    return name.replace("-", "_")


@dataclass(frozen=True)
class InstalledCheckReport:
    findings: tuple[Finding, ...]
    cli_path: str | None
    native_module_file: str | None


def _venv_bin_dir(venv_dir: Path) -> Path:
    scripts = venv_dir / "Scripts"
    return scripts if scripts.is_dir() else venv_dir / "bin"


def _venv_python(venv_dir: Path) -> Path:
    bin_dir = _venv_bin_dir(venv_dir)
    candidate = bin_dir / "python.exe"
    if candidate.is_file():
        return candidate
    return bin_dir / "python3"


def check_installed(ci: CiToml, repo_root: Path, venv_dir: Path) -> InstalledCheckReport:  # noqa: C901
    findings: list[Finding] = []
    cli_name = ci.python.cli.name if ci.python is not None else ""
    bin_dir = _venv_bin_dir(venv_dir)

    cli_path = next((p for p in (bin_dir / cli_name, bin_dir / f"{cli_name}.exe") if p.is_file()), None)
    if cli_path is None:
        findings.append(
            Finding(
                rule="PKG-003",
                path=str(bin_dir),
                message=f"no '{cli_name}[.exe]' found in the venv's {bin_dir.name}/ directory",
                fix=f"install the built wheel into this venv first ('uv pip install <wheel>'); the "
                f"bundled binary should land in {bin_dir}",
            )
        )
    else:
        data = cli_path.read_bytes()
        host = _host_family_and_arch()
        if host is None:
            findings.append(
                Finding(
                    rule="PKG-003",
                    status=Status.NEEDS_REVIEW,
                    path=str(cli_path),
                    message=f"cannot determine this host's architecture from sys.platform="
                    f"{sys.platform!r}/platform.machine()={host_platform.machine()!r}",
                    fix="run on a host ci-lint recognizes (linux/windows/macos x86_64/aarch64)",
                )
            )
        else:
            magic = check_magic(data, host.family, host.arch)
            ok, detail = magic.ok, magic.detail
            if not ok:
                findings.append(
                    Finding(
                        rule="PKG-003",
                        path=str(cli_path),
                        message=f"installed '{cli_path.name}' magic bytes don't match this host: {detail}",
                        fix="reinstall the wheel built for this host's platform",
                    )
                )
        try:
            proc = run_captured([str(cli_path), "--version"], timeout=30)
        except OSError as exc:
            findings.append(
                Finding(
                    rule="PKG-003",
                    path=str(cli_path),
                    message=f"could not execute '{cli_path}': {exc}",
                    fix="ensure the installed binary is executable and not quarantined by the OS",
                )
            )
        else:
            if proc.returncode != 0:
                findings.append(
                    Finding(
                        rule="PKG-003",
                        path=str(cli_path),
                        message=f"'{cli_path.name} --version' exited {proc.returncode} "
                        f"(stderr: {proc.stderr.strip()[:200]})",
                        fix="fix the CLI so '--version' exits 0",
                    )
                )

    native_module_file: str | None = None
    pkg_name = _python_package_name(repo_root)
    if pkg_name is None:
        findings.append(
            Finding(
                rule="PKG-005",
                status=Status.NEEDS_REVIEW,
                path="pyproject.toml",
                message="cannot determine the installed Python package name from pyproject.toml's "
                "[project].name",
                fix="ensure pyproject.toml declares [project].name",
            )
        )
    else:
        venv_python = _venv_python(venv_dir)
        code = f"import {pkg_name}, {pkg_name}._native as n; print(n.__file__)"
        try:
            proc = run_captured([str(venv_python), "-c", code], timeout=30)
        except OSError as exc:
            findings.append(
                Finding(
                    rule="PKG-005",
                    path=str(venv_python),
                    message=f"could not run the venv's python: {exc}",
                    fix=f"ensure {venv_python} exists (the --venv directory must be a real virtualenv)",
                )
            )
        else:
            if proc.returncode != 0:
                findings.append(
                    Finding(
                        rule="PKG-005",
                        path=pkg_name,
                        message=f"'import {pkg_name}, {pkg_name}._native' failed: "
                        f"{proc.stderr.strip()[:300]}",
                        fix="ensure the installed wheel bundles the compiled '_native' extension and "
                        "it imports cleanly (no pure-Python fallback masking a missing extension)",
                    )
                )
            else:
                native_module_file = proc.stdout.strip()
                if not native_module_file.endswith((".so", ".pyd", ".dylib")) and ".abi3." not in native_module_file:
                    findings.append(
                        Finding(
                            rule="PKG-005",
                            path=native_module_file,
                            message=f"'{pkg_name}._native.__file__' = {native_module_file!r}, which is "
                            "not a compiled-extension suffix",
                            fix="ensure '_native' resolves to the compiled PyO3 extension module, not a "
                            "pure-Python shim or namespace package",
                        )
                    )

    return InstalledCheckReport(
        findings=tuple(findings), cli_path=str(cli_path) if cli_path else None, native_module_file=native_module_file
    )


def _findings_json(findings: tuple[Finding, ...]) -> list[dict[str, JsonValue]]:
    return [
        {"rule": f.rule, "status": f.status.value, "path": f.path, "line": f.line, "message": f.message, "fix": f.fix}
        for f in findings
    ]


def wheel_check_to_json_dict(report: WheelCheckReport) -> dict[str, JsonValue]:
    return {
        "wheel_path": report.wheel_path,
        "generator": report.generator,
        "script_member": report.script_member,
        "native_member": report.native_member,
        "findings": _findings_json(report.findings),
    }


def installed_check_to_json_dict(report: InstalledCheckReport) -> dict[str, JsonValue]:
    return {
        "cli_path": report.cli_path,
        "native_module_file": report.native_module_file,
        "findings": _findings_json(report.findings),
    }


def render_wheel_check(report: WheelCheckReport) -> str:
    lines = [f"ci-lint wheel check: {report.wheel_path}"]
    lines.append(f"  script entry:  {report.script_member or '(none)'}")
    lines.append(f"  native entry:  {report.native_member or '(none)'}")
    lines.append(f"  WHEEL Generator: {report.generator!r}")
    lines.append("")
    for f in report.findings:
        lines.append(f.render())
    n = sum(1 for f in report.findings if f.status == Status.VIOLATION)
    lines.append(f"ci-lint wheel check: {n} violation(s)")
    return "\n".join(lines)


def render_installed_check(report: InstalledCheckReport) -> str:
    lines = [
        "ci-lint wheel installed:",
        f"  cli:            {report.cli_path or '(not found)'}",
        f"  native module:  {report.native_module_file or '(not found)'}",
        "",
    ]
    for f in report.findings:
        lines.append(f.render())
    n = sum(1 for f in report.findings if f.status == Status.VIOLATION)
    lines.append(f"ci-lint wheel installed: {n} violation(s)")
    return "\n".join(lines)
