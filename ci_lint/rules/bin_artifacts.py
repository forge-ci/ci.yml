"""BIN-001, BIN-002 (round M2-20, zackees/ci.yml#44 part A): per
docs/policy-rust.md, a repository that ships a native-binary app must
declare both a Linux libc variant floor and every required Windows
ABI/architecture combination as distinct `[platforms]` entries -- these
rules only apply once `[python].cli.native` is true (a repository whose
CLI is not a native binary has nothing to check here).

BIN-001: at least one `group = "linux"` platform's `target` must be a
`*-unknown-linux-musl` triple (the portable floor), or the repository must
carry a `[[exceptions]]` entry recording why glibc-only is acceptable.

BIN-002: every declared `group = "windows"` platform must use the MSVC ABI
(`*-pc-windows-msvc`, never `-gnu`), and `x86_64`/`aarch64` must each map to
their own platform id -- a target string with the wrong arch-in-triple vs.
arch-in-id (e.g. an id named `windows-arm64` whose target is the x64
triple) is flagged as a substitution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.schema import CiToml

_MUSL_SUFFIX = "-unknown-linux-musl"
_MSVC_SUFFIX = "-pc-windows-msvc"
_GNU_WINDOWS_SUFFIX = "-pc-windows-gnu"

@dataclass(frozen=True)
class _ArchHint:
    id_hint: str
    arch_prefix: str


_ARCH_HINTS: tuple[_ArchHint, ...] = (
    _ArchHint("arm64", "aarch64"),
    _ArchHint("x64", "x86_64"),
)


def _applies(ci: CiToml) -> bool:
    return ci.python is not None and ci.python.cli.native


def check_bin_001(ci: CiToml) -> list[Finding]:
    if not _applies(ci):
        return []
    linux_platforms = [p for p in ci.platforms.values() if p.group == "linux"]
    if not linux_platforms:
        return []
    if any(p.target.endswith(_MUSL_SUFFIX) for p in linux_platforms):
        return []
    return [
        Finding(
            rule="BIN-001",
            path="ci.toml#platforms",
            message=f"[python].cli.native = true, but no group = \"linux\" platform's target "
            f"ends with '{_MUSL_SUFFIX}' (found: "
            f"{sorted(p.target for p in linux_platforms)})",
            fix=f"add a '[platforms.<id>]' entry with target = \"<arch>{_MUSL_SUFFIX}\" (the "
            "portable Linux floor), or record a '[[exceptions]]' entry (rule = \"BIN-001\") in "
            "ci.toml with an issue link, owner and review date explaining why glibc-only is "
            "acceptable",
        )
    ]


def check_bin_002(ci: CiToml) -> list[Finding]:
    if not _applies(ci):
        return []
    windows_platforms = {pid: p for pid, p in ci.platforms.items() if p.group == "windows"}
    if not windows_platforms:
        return []
    findings: list[Finding] = []
    for pid, plat in sorted(windows_platforms.items()):
        loc = f"ci.toml#platforms.{pid}"
        if plat.target.endswith(_GNU_WINDOWS_SUFFIX):
            findings.append(
                Finding(
                    rule="BIN-002",
                    path=loc,
                    message=f"platforms.{pid}.target = '{plat.target}' uses the GNU Windows ABI",
                    fix=f"change platforms.{pid}.target to the MSVC triple "
                    f"(replace '{_GNU_WINDOWS_SUFFIX}' with '{_MSVC_SUFFIX}'); the fleet ships "
                    "MSVC Windows binaries, never MinGW/GNU",
                )
            )
            continue
        if not plat.target.endswith(_MSVC_SUFFIX):
            findings.append(
                Finding(
                    rule="BIN-002",
                    path=loc,
                    message=f"platforms.{pid}.target = '{plat.target}' is a group = \"windows\" "
                    f"platform but does not end with '{_MSVC_SUFFIX}'",
                    fix=f"set platforms.{pid}.target to \"<arch>{_MSVC_SUFFIX}\"",
                )
            )
            continue
        # ABI is correct MSVC; now check the id's declared architecture
        # (round-M2-20: an id named e.g. "windows-arm64" must carry the
        # aarch64 triple, never the x64 one, and vice versa -- a swap is a
        # silent substitution that the profile-required-table check alone
        # cannot see, since both are valid MSVC targets).
        for hint in _ARCH_HINTS:
            id_hint, arch_prefix = hint.id_hint, hint.arch_prefix
            if id_hint in pid.lower() and not plat.target.startswith(f"{arch_prefix}-"):
                findings.append(
                    Finding(
                        rule="BIN-002",
                        path=loc,
                        message=f"platforms.{pid}.target = '{plat.target}' does not match the "
                        f"architecture implied by its id ('{id_hint}' expects an "
                        f"'{arch_prefix}-' triple)",
                        fix=f"fix platforms.{pid}.target to the '{arch_prefix}-{_MSVC_SUFFIX.lstrip('-')}' "
                        f"triple, or rename the platform id to match the target it actually builds",
                    )
                )
    has_x64 = any("x64" in pid.lower() for pid in windows_platforms)
    has_arm64 = any("arm64" in pid.lower() for pid in windows_platforms)
    if not (has_x64 and has_arm64):
        findings.append(
            Finding(
                rule="BIN-002",
                path="ci.toml#platforms",
                message="group = \"windows\" platforms do not cover both x64 and arm64 "
                f"(found: {sorted(windows_platforms)})",
                fix="declare both a 'windows-x64' and a 'windows-arm64' platform (MSVC triples), "
                "or record a '[[exceptions]]' entry (rule = \"BIN-002\") with an issue link, "
                "owner and review date",
            )
        )
    return findings


def check_group_bin(ci: CiToml, repo_root: Path) -> list[Finding]:
    del repo_root  # both checks are pure ci.toml inspection today
    findings: list[Finding] = []
    findings.extend(check_bin_001(ci))
    findings.extend(check_bin_002(ci))
    return findings
