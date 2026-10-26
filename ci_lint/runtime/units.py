"""`ci-lint units`: compile-unit-explosion check from a real build
(round-2A brief, part 2a).

Static precheck (`ci_lint.rules.rust_units`) can prove a private crate has
no `[features]` and no `cfg(feature)`, and that no command line names an
undeclared `--features` set -- but it cannot see *how many times* Cargo's
feature unification actually compiled each crate in one invocation. This
module reads the compiler-artifact records a real `cargo ... --message-
format=json` run produced and counts, per workspace member, the distinct
`(sorted features, profile, target kind, target triple)` combinations that
were actually compiled -- the runtime half of `RUST-011`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import CompilerArtifact, JsonValue
from ci_lint.cargo_scan import CargoCrate, effective_features
from ci_lint.finding import Finding
from ci_lint.globs import matches_any
from ci_lint.schema import CiToml


@dataclass(frozen=True)
class CrateUnit:
    """One distinct combination actually compiled for a crate."""

    features: tuple[str, ...]
    profile: str
    target_kind: str
    target_triple: str
    occurrences: int


@dataclass(frozen=True)
class CrateUnitsReport:
    package: str
    units: tuple[CrateUnit, ...]  # sorted, distinct


@dataclass(frozen=True)
class UnitsReport:
    crates: tuple[CrateUnitsReport, ...]
    findings: tuple[Finding, ...]
    unmatched_packages: tuple[str, ...]  # artifact package names not a workspace member


@dataclass(frozen=True, order=True)
class _UnitKey:
    """A distinct compilation unit of one workspace member."""

    package: str
    features: tuple[str, ...]
    profile: str
    target_kind: str
    target_triple: str


def _profile_key(a: CompilerArtifact) -> str:
    return f"opt={a.opt_level}/debug={a.debuginfo}/test={a.profile_test}"


def _target_triple(declared_targets: frozenset[str], filenames: tuple[str, ...]) -> str:
    """A target triple is only knowable from the build *output path*
    (`target/<triple>/<profile>/...` for a cross build vs.
    `target/<profile>/...` for a host build) -- cargo's JSON message never
    names it directly. Matching path components against `ci.toml`'s
    declared `[platforms].*.target` set (rather than a generic
    triple-shaped regex) means this never guesses at a triple ci.toml does
    not even know about."""

    for filename in filenames:
        for part in Path(filename).parts:
            if part in declared_targets:
                return part
    return "host"


def compute_units(ci: CiToml, crates: list[CargoCrate], artifacts: list[CompilerArtifact]) -> UnitsReport:
    member_names = {c.name for c in crates}
    declared_targets = frozenset(p.target for p in ci.platforms.values())

    counts: dict[_UnitKey, int] = {}
    unmatched: set[str] = set()
    for a in artifacts:
        if a.package_name not in member_names:
            if a.package_name:
                unmatched.add(a.package_name)
            continue
        target_kind = ",".join(sorted(a.target_kinds)) or "unknown"
        triple = _target_triple(declared_targets, a.filenames)
        key = _UnitKey(a.package_name, a.features, _profile_key(a), target_kind, triple)
        counts[key] = counts.get(key, 0) + 1

    crate_reports: list[CrateUnitsReport] = []
    for name in sorted({k.package for k in counts}):
        units = tuple(
            CrateUnit(
                features=k.features, profile=k.profile, target_kind=k.target_kind, target_triple=k.target_triple,
                occurrences=v,
            )
            for k, v in sorted(counts.items())
            if k.package == name
        )
        crate_reports.append(CrateUnitsReport(package=name, units=units))

    findings: list[Finding] = list(_rust_011_findings(ci, crates, crate_reports))
    return UnitsReport(
        crates=tuple(crate_reports), findings=tuple(findings), unmatched_packages=tuple(sorted(unmatched))
    )


def _rust_011_findings(
    ci: CiToml, crates: list[CargoCrate], crate_reports: list[CrateUnitsReport]
) -> list[Finding]:
    findings: list[Finding] = []
    if ci.rust is None:
        return findings

    private_names = {c.name for c in crates if matches_any(c.dir, (ci.rust.private,))}
    public_crate = next((c for c in crates if c.dir == ci.rust.public), None)
    declared = public_crate.features if public_crate is not None else {}
    ship_sets = {effective_features(s, declared) for s in ci.rust.ship} if ci.rust.ship else set()

    by_package = {r.package: r for r in crate_reports}

    for name in sorted(private_names & set(by_package)):
        report = by_package[name]
        by_profile: dict[str, set[tuple[str, ...]]] = {}
        for u in report.units:
            by_profile.setdefault(u.profile, set()).add(u.features)
        for profile in sorted(by_profile):
            feature_sets = by_profile[profile]
            if len(feature_sets) > 1:
                findings.append(
                    Finding(
                        rule="RUST-011",
                        message=(
                            f"private crate '{name}' compiled with {len(feature_sets)} distinct "
                            f"feature sets in profile {profile}: "
                            f"{sorted(sorted(f) for f in feature_sets)}"
                        ),
                        fix=(
                            f"private crates must compile with exactly one (normally empty) feature "
                            f"set per profile; find the '-p {name}' invocations passing different "
                            "--features and make them agree (a private crate has no [features] to "
                            "unify on in the first place -- see [rust].private in ci.toml)"
                        ),
                    )
                )

    if public_crate is not None and public_crate.name in by_package:
        report = by_package[public_crate.name]
        seen_bad: set[tuple[str, ...]] = set()
        for u in report.units:
            if effective_features(u.features, declared) not in ship_sets and u.features not in seen_bad:
                seen_bad.add(u.features)
                findings.append(
                    Finding(
                        rule="RUST-011",
                        path=public_crate.manifest_path,
                        message=(
                            f"public crate '{public_crate.name}' compiled with feature set "
                            f"{list(u.features)}, which is not one of [rust].ship "
                            f"{[sorted(s) for s in ship_sets]}"
                        ),
                        fix=(
                            "only compile the amalgam crate with a feature set declared in ci.toml's "
                            "[rust].ship; add this set to [rust].ship if it is genuinely a shipped "
                            "combination, or fix the command line that produced it"
                        ),
                    )
                )
    return findings


def to_json_dict(report: UnitsReport) -> dict[str, JsonValue]:
    return {
        "crates": [
            {
                "package": c.package,
                "units": [
                    {
                        "features": list(u.features),
                        "profile": u.profile,
                        "target_kind": u.target_kind,
                        "target_triple": u.target_triple,
                        "occurrences": u.occurrences,
                    }
                    for u in c.units
                ],
            }
            for c in report.crates
        ],
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "line": f.line, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
        "unmatched_packages": list(report.unmatched_packages),
    }


def render_text(report: UnitsReport) -> str:
    lines: list[str] = []
    if not report.crates:
        lines.append("ci-lint units: no workspace-member compiler-artifact records found.")
    for crate in report.crates:
        lines.append(f"== {crate.package} ({len(crate.units)} distinct unit(s)) ==")
        lines.append(f"{'features':<30} {'profile':<28} {'kind':<10} {'triple':<28} occurrences")
        for u in crate.units:
            feats = ",".join(u.features) or "-"
            lines.append(f"{feats:<30} {u.profile:<28} {u.target_kind:<10} {u.target_triple:<28} {u.occurrences}")
        lines.append("")
    if report.unmatched_packages:
        lines.append(
            "(ignored, not a workspace member: " + ", ".join(report.unmatched_packages) + ")"
        )
        lines.append("")
    for f in report.findings:
        lines.append(f.render())
    n_violations = len(report.findings)
    lines.append(f"ci-lint units: {n_violations} violation(s) across {len(report.crates)} crate(s)")
    return "\n".join(lines)
