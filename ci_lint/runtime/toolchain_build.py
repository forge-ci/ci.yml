"""`ci-lint rust toolchain-build-check`: RUST-009 from real job-log
evidence (issue #5, M2-22 brief).

"Per-run toolchain/std/driver builds detected from job logs: `Compiling
core`/`std`, -Zbuild-std, dylint-driver source builds,
SOLDR_ALLOW_DYLINT_DRIVER_BUILD." Like `ci_lint.runtime.dylint`
(RUST-003), this is deliberately NOT a static check -- whether a run
actually compiled std/core/the Dylint driver from source can only be
proven by reading what that run's build actually printed, not by
inspecting `ci.toml`/workflow YAML. It reads one or more `--log` files
(raw text: `gh run view --job <id> --log`, or any captured stdout).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status

_ANSI_RE = re.compile(r"(?:\x1b\[|\^\[\[)[0-9;]*[A-Za-z]")

# `cargo`'s own "Compiling <crate> v<version>" trace line for the crates
# that make up the standard library / compiler-builtins -- these are only
# ever compiled from source when a per-run -Zbuild-std (or an unvendored
# toolchain missing the prebuilt target std) forces it.
_COMPILING_STD_RE = re.compile(
    r"^\s*Compiling (core|std|alloc|compiler_builtins) v", re.MULTILINE
)
_BUILD_STD_FLAG_RE = re.compile(r"-Z\s*build-std\b")
# Cargo prints the driver's package name as `dylint_driver`; retain the
# hyphenated spelling used by existing setup/tool diagnostics too.
_DRIVER_SOURCE_BUILD_RE = re.compile(
    r"Compiling dylint[-_]driver(?=\s|$)|Building dylint-driver from source|"
    r"error: dylint-driver .* not found.*building from source",
    re.IGNORECASE,
)
_ALLOW_DRIVER_BUILD_ENV_RE = re.compile(
    r"SOLDR_ALLOW_DYLINT_DRIVER_BUILD\s*=\s*(1|[Tt]rue)"
)


class ToolchainBuildCheckError(Exception):
    """An unreadable `--log` file -- exit 2, never silently treated as
    "no evidence" (matching `ci_lint.runtime.dylint`'s convention)."""


@dataclass(frozen=True)
class ToolchainBuildReport:
    sources: tuple[str, ...]
    findings: tuple[Finding, ...]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolchainBuildCheckError(f"cannot read --log {path}: {exc}") from exc


def _findings_for_log(path: Path, text: str) -> list[Finding]:
    clean = _ANSI_RE.sub("", text)
    findings: list[Finding] = []

    std_matches = sorted({m.group(1) for m in _COMPILING_STD_RE.finditer(clean)})
    if std_matches:
        findings.append(
            Finding(
                rule="RUST-009",
                path=str(path),
                message=(
                    f"per-run standard-library build detected: 'Compiling {std_matches[0]}' "
                    f"({', '.join(std_matches)}) was compiled from source this run instead of "
                    "coming from a prebuilt catalogued toolchain target"
                ),
                fix="provision the target's prebuilt rust-std ahead of the build ('soldr dylint "
                "prepare --target T' / setup-soldr's toolchain input), and drop any -Zbuild-std "
                "flag forcing a from-source std/core/alloc/compiler_builtins compile every run",
            )
        )

    if _BUILD_STD_FLAG_RE.search(clean):
        findings.append(
            Finding(
                rule="RUST-009",
                path=str(path),
                message="a '-Zbuild-std' flag was present in this run's build invocation, forcing "
                "a per-run standard-library rebuild",
                fix="remove -Zbuild-std from the build/check/test invocation; use the prebuilt "
                "catalogued target std/core instead of building the toolchain per run",
            )
        )

    if _DRIVER_SOURCE_BUILD_RE.search(clean):
        findings.append(
            Finding(
                rule="RUST-009",
                path=str(path),
                message="the Dylint driver was built from source this run instead of coming from "
                "a catalogued prebuilt (dylint-toolchain pin)",
                fix="pin 'dylint-toolchain' to a catalogued prebuilt driver version in the "
                "zackees/setup-soldr call, so 'soldr dylint prepare' resolves it instead of "
                "compiling dylint-driver from source on every run",
            )
        )

    allow_env_matches = _ALLOW_DRIVER_BUILD_ENV_RE.findall(clean)
    if allow_env_matches and not _DRIVER_SOURCE_BUILD_RE.search(clean):
        findings.append(
            Finding(
                rule="RUST-009",
                path=str(path),
                status=Status.NEEDS_REVIEW,
                message="SOLDR_ALLOW_DYLINT_DRIVER_BUILD is set truthy in this run's evidence, but "
                "no explicit driver source-build trace was matched -- confirm this escape hatch is "
                "actually needed and not left on routinely",
                fix="unset SOLDR_ALLOW_DYLINT_DRIVER_BUILD once the catalogued prebuilt driver "
                "covers this dylint-toolchain pin again; it must be an explicit, reviewed, "
                "temporary override, never a standing default",
            )
        )

    return findings


def compute_toolchain_build_report(log_paths: list[Path]) -> ToolchainBuildReport:
    findings: list[Finding] = []
    for path in log_paths:
        findings.extend(_findings_for_log(path, _read(path)))
    return ToolchainBuildReport(sources=tuple(str(p) for p in log_paths), findings=tuple(findings))


def to_json_dict(report: ToolchainBuildReport) -> dict[str, JsonValue]:
    return {
        "sources": list(report.sources),
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: ToolchainBuildReport) -> str:
    lines = [f"ci-lint rust toolchain-build-check: {len(report.sources)} log source(s)"]
    if not report.findings:
        lines.append("no findings.")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint rust toolchain-build-check: {len(report.findings)} finding(s)")
    return "\n".join(lines)
