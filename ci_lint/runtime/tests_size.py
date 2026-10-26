"""`ci-lint tests size`: test-binary declaration + size budget from a real
build (round-2A brief, part 2b).

Reads the same `cargo ... --message-format=json` artifacts file as
`ci-lint units`, keeps only records with `profile.test == true` and an
`executable`, maps each one to the `[rust.tests].binaries` naming scheme
(`<crate>:lib`, `<crate>:bin:<name>`, `<crate>:test:<name>`), stats the
file on disk, and checks it against `RUST-012`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import CompilerArtifact, JsonValue
from ci_lint.finding import Finding, Status
from ci_lint.rules.cache_static import parse_size
from ci_lint.schema import CiToml


@dataclass(frozen=True)
class TestBinarySize:
    name: str
    bytes: int
    executable: str


@dataclass(frozen=True)
class TestsSizeReport:
    binaries: tuple[TestBinarySize, ...]
    total_bytes: int
    max_binary_bytes: int | None
    max_total_bytes: int | None
    findings: tuple[Finding, ...]


def _declared_name(package: str, target_kinds: tuple[str, ...], target_name: str) -> str | None:
    if "lib" in target_kinds:
        return f"{package}:lib"
    if "bin" in target_kinds:
        return f"{package}:bin:{target_name}"
    if "test" in target_kinds:
        return f"{package}:test:{target_name}"
    return None


def compute_tests_size(ci: CiToml, artifacts: list[CompilerArtifact]) -> TestsSizeReport:
    declared: set[str] = set(ci.rust.tests.binaries) if ci.rust and ci.rust.tests else set()
    max_binary = parse_size(ci.rust.tests.max_binary) if ci.rust and ci.rust.tests else None
    max_total = parse_size(ci.rust.tests.max_total) if ci.rust and ci.rust.tests else None

    findings: list[Finding] = []
    seen: dict[str, TestBinarySize] = {}

    for a in artifacts:
        if not a.profile_test or not a.executable:
            continue
        name = _declared_name(a.package_name, a.target_kinds, a.target_name)
        if name is None:
            continue
        try:
            size = Path(a.executable).stat().st_size
        except OSError as exc:
            findings.append(
                Finding(
                    rule="RUST-012",
                    status=Status.NEEDS_REVIEW,
                    path=a.executable,
                    message=f"cannot stat the executable recorded for '{name}': {exc}",
                    fix=(
                        "run 'ci-lint tests size' with --artifacts captured from a real build in this "
                        "checkout, so every recorded executable path still exists on disk"
                    ),
                )
            )
            continue
        seen[name] = TestBinarySize(name=name, bytes=size, executable=a.executable)

        if name not in declared:
            findings.append(
                Finding(
                    rule="RUST-012",
                    message=f"undeclared test binary '{name}' ({size} B, from {a.executable})",
                    fix=f'add "{name}" to [rust.tests].binaries in ci.toml, or make the target not '
                    "produce a harness (e.g. 'test = false')",
                )
            )
        if max_binary is not None and size > max_binary:
            findings.append(
                Finding(
                    rule="RUST-012",
                    message=f"test binary '{name}' is {size} B, over [rust.tests].max-binary ({max_binary} B)",
                    fix=f"shrink '{name}' (split debug info, fewer dependencies, thinner test harness) "
                    "or raise [rust.tests].max-binary in ci.toml if the size is genuinely necessary",
                )
            )

    for name in sorted(declared - seen.keys()):
        findings.append(
            Finding(
                rule="RUST-012",
                path="ci.toml",
                message=f"declared test binary '{name}' was not produced by this build",
                fix=f'remove "{name}" from [rust.tests].binaries in ci.toml, or fix the Cargo target so '
                "it actually produces this harness",
            )
        )

    total = sum(b.bytes for b in seen.values())
    if max_total is not None and total > max_total:
        findings.append(
            Finding(
                rule="RUST-012",
                message=f"sum of test-binary sizes is {total} B, over [rust.tests].max-total ({max_total} B)",
                fix="shrink test binaries or raise [rust.tests].max-total in ci.toml",
            )
        )

    binaries = tuple(seen[n] for n in sorted(seen))
    return TestsSizeReport(
        binaries=binaries,
        total_bytes=total,
        max_binary_bytes=max_binary,
        max_total_bytes=max_total,
        findings=tuple(findings),
    )


def to_json_dict(report: TestsSizeReport) -> dict[str, JsonValue]:
    return {
        "binaries": [{"name": b.name, "bytes": b.bytes, "executable": b.executable} for b in report.binaries],
        "total_bytes": report.total_bytes,
        "max_binary_bytes": report.max_binary_bytes,
        "max_total_bytes": report.max_total_bytes,
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "line": f.line, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: TestsSizeReport) -> str:
    lines: list[str] = ["ci-lint tests size:", f"{'name':<40} {'bytes':>12}  cap"]
    cap_str = f"{report.max_binary_bytes} B" if report.max_binary_bytes is not None else "(unset)"
    for b in report.binaries:
        lines.append(f"{b.name:<40} {b.bytes:>12}  {cap_str}")
    total_cap = f"{report.max_total_bytes} B" if report.max_total_bytes is not None else "(unset)"
    lines.append(f"{'TOTAL':<40} {report.total_bytes:>12}  {total_cap}")
    lines.append("")
    for f in report.findings:
        lines.append(f.render())
    lines.append(f"ci-lint tests size: {len(report.findings)} violation(s)")
    return "\n".join(lines)
